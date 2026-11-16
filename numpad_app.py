"""Web Numpad — окно для Windows: QR-код для телефона, статус и настройки.

Собирается в WebNumpad.exe скриптом build.ps1. Запуск без сборки: python numpad_app.py
"""
import argparse
import atexit
import ctypes
import io
import os
import queue
import sys
import time
import tkinter as tk
from ctypes import wintypes
from tkinter import messagebox, ttk

import segno

import numpad_core as core

TITLE = core.APP_NAME
COLORS = {
    "bg": "#121318", "panel": "#1a1c23", "line": "#2a2e39", "text": "#eef0f4", "sub": "#8b92a3",
    "accent": "#ffb224", "ok": "#3ddc84", "bad": "#ff5d5d", "qr_dark": "#0e0f13", "qr_light": "#ffffff",
}
FONT = "Segoe UI"


# ---------------------------------------------------------------------------
# Windows: один экземпляр, иконка на панели задач, чёткий шрифт
# ---------------------------------------------------------------------------
def single_instance_or_focus():
    """True — мы первый экземпляр. Иначе показать уже открытое окно и вернуть False."""
    if os.name != "nt":
        return True
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = wintypes.HANDLE
    handle = k32.CreateMutexW(None, False, "Local\\WebNumpad.SingleInstance")
    if ctypes.get_last_error() != 183:  # ERROR_ALREADY_EXISTS
        single_instance_or_focus.handle = handle  # держим, пока процесс жив
        return True
    u32 = ctypes.WinDLL("user32")
    hwnd = u32.FindWindowW(None, TITLE)
    if hwnd:
        u32.ShowWindow(hwnd, 9)  # SW_RESTORE
        u32.SetForegroundWindow(hwnd)
    return False


def windows_tweaks():
    if os.name != "nt":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("WebNumpad.App")
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Окно
# ---------------------------------------------------------------------------
class App:
    NET_CHECK_MS = 4000

    def __init__(self, args):
        self.args = args
        self.cfg = core.Config(args.config_dir)
        self.events = queue.Queue()
        self.log_path = os.path.join(self.cfg.folder, "log.txt")
        self._trim_log()

        self.injector = core.Injector(dry_run=args.dry_run, log=self.log)
        self.hub = core.Hub(self.injector)
        atexit.register(self.hub.release_all)
        self.server = core.Server(self.cfg["pin"], self.hub, log=self.log, on_event=self._server_event)
        self.thread = core.ServerThread(self.server)

        self.port = args.port or int(self.cfg["port"])
        self.bound_hosts = []
        self.qr_host = None
        self.phones = []
        self.state = "starting"
        self.state_detail = ""

        self.root = tk.Tk()
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        self.root.after(100, self._pump)
        self.root.after(50, self.restart_server)
        self.root.after(self.NET_CHECK_MS, self._net_watch)
        if args.autostart and self.cfg["start_minimized"]:
            self.root.after(400, self.root.iconify)

    # ---- журнал ----
    def _trim_log(self):
        try:
            if os.path.getsize(self.log_path) > 512 * 1024:
                with open(self.log_path, "rb") as f:
                    f.seek(-128 * 1024, os.SEEK_END)
                    tail = f.read()
                with open(self.log_path, "wb") as f:
                    f.write(tail)
        except OSError:
            pass

    def log(self, msg):
        line = f"{time.strftime('%H:%M:%S')}  {msg}"
        try:
            os.makedirs(self.cfg.folder, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d ") + line + "\n")
        except OSError:
            pass
        self.events.put(("log", line))

    def _server_event(self, kind, **data):
        self.events.put((kind, data))

    def _pump(self):
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == "log":
                    self._append_log(data)
                elif kind == "phones":
                    self.phones = data["phones"]
                    self._paint_status()
        except queue.Empty:
            pass
        self.root.after(100, self._pump)

    # ---- сервер ----
    def desired_hosts(self):
        lan = core.lan_addresses()
        host = self.cfg["host"]
        if host and host != "auto":
            return [host] if host in lan else [], lan
        return lan, lan

    def restart_server(self, quiet=False):
        hosts, lan = self.desired_hosts()
        self._fill_hosts(lan)
        if not hosts:
            self.thread.stop()
            self.bound_hosts = []
            self.qr_host = None
            want = self.cfg["host"]
            self._set_state("wait", f"Нет адреса {want} в сети — жду подключения…" if want != "auto"
                            else "Нет подключения к домашней сети — жду…")
            self._paint_qr()
            return
        try:
            bound, errors = self.thread.start(hosts, self.port)
        except Exception as e:  # noqa: BLE001
            bound, errors = [], [("?", e)]
        self.bound_hosts = [h for h in bound if h != "127.0.0.1"]
        self.qr_host = self.bound_hosts[0] if self.bound_hosts else None
        if self.bound_hosts:
            self._set_state("ok", "")
            self.log(f"сервер слушает {', '.join(f'{h}:{self.port}' for h in self.bound_hosts)}")
        else:
            previous = self.state_detail if self.state == "bad" else None
            busy = any(getattr(e, "winerror", None) == 10048 or getattr(e, "errno", None) == 10048 for _, e in errors)
            if busy:
                self._set_state("bad", f"Порт {self.port} занят другой программой. Закройте её "
                                       "(например, консольный Web Numpad) или смените порт.")
            else:
                self._set_state("bad", "Не удалось открыть порт: " + "; ".join(f"{h}: {e}" for h, e in errors))
            if not quiet or self.state_detail != previous:
                self.log(self.state_detail + "  Повторяю попытку каждые несколько секунд.")
        self._paint_qr()

    def _net_watch(self):
        hosts, lan = self.desired_hosts()
        self._fill_hosts(lan)
        if self.state == "bad":
            self.restart_server(quiet=True)  # например, порт освободился после закрытия консольного варианта
        elif sorted(hosts) != sorted(self.bound_hosts) and (hosts or self.bound_hosts):
            self.log("сетевой адрес изменился — перезапускаю сервер")
            self.restart_server()
        self.root.after(self.NET_CHECK_MS, self._net_watch)

    # ---- интерфейс ----
    def _build_ui(self):
        r = self.root
        r.title(TITLE)
        r.configure(bg=COLORS["bg"])
        r.minsize(760, 470)
        try:
            self._icon = tk.PhotoImage(data=core.make_icon(64))
            r.iconphoto(True, self._icon)
        except tk.TclError:
            pass

        style = ttk.Style(r)
        style.theme_use("clam")
        c = COLORS
        style.configure(".", background=c["bg"], foreground=c["text"], font=(FONT, 10))
        style.configure("Panel.TFrame", background=c["panel"])
        style.configure("TFrame", background=c["bg"])
        style.configure("TLabel", background=c["bg"], foreground=c["text"])
        style.configure("Panel.TLabel", background=c["panel"], foreground=c["text"])
        style.configure("Sub.TLabel", background=c["panel"], foreground=c["sub"], font=(FONT, 9))
        style.configure("Head.TLabel", background=c["panel"], foreground=c["sub"], font=(FONT, 9, "bold"))
        style.configure("Pin.TLabel", background=c["bg"], foreground=c["text"], font=(FONT, 22, "bold"))
        style.configure("Url.TLabel", background=c["bg"], foreground=c["sub"], font=(FONT, 9))
        style.configure("TButton", background=c["line"], foreground=c["text"], borderwidth=0,
                        focuscolor=c["line"], padding=(12, 7))
        style.map("TButton", background=[("active", "#353a47"), ("pressed", "#40465a")])
        style.configure("Accent.TButton", background=c["accent"], foreground="#1a1300", font=(FONT, 10, "bold"))
        style.map("Accent.TButton", background=[("active", "#ffc24d"), ("pressed", "#e09a10")])
        style.configure("TCheckbutton", background=c["panel"], foreground=c["text"], focuscolor=c["panel"],
                        indicatorbackground=c["bg"], indicatorforeground=c["accent"])
        style.map("TCheckbutton", background=[("active", c["panel"])],
                  indicatorbackground=[("selected", c["bg"])])
        style.configure("TCombobox", fieldbackground=c["bg"], background=c["line"], foreground=c["text"],
                        arrowcolor=c["text"], bordercolor=c["line"], lightcolor=c["line"], darkcolor=c["line"])
        style.map("TCombobox", fieldbackground=[("readonly", c["bg"])], foreground=[("readonly", c["text"])],
                  selectbackground=[("readonly", c["bg"])], selectforeground=[("readonly", c["text"])])
        style.configure("TEntry", fieldbackground=c["bg"], foreground=c["text"], insertcolor=c["text"],
                        bordercolor=c["line"], lightcolor=c["line"], darkcolor=c["line"])
        r.option_add("*TCombobox*Listbox.background", c["panel"])
        r.option_add("*TCombobox*Listbox.foreground", c["text"])
        r.option_add("*TCombobox*Listbox.selectBackground", c["accent"])
        r.option_add("*TCombobox*Listbox.selectForeground", "#1a1300")

        outer = ttk.Frame(r, padding=18)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(0, weight=1)

        # --- слева: QR и код ---
        left = ttk.Frame(outer)
        left.grid(row=0, column=0, sticky="n", padx=(0, 18))
        self.qr_label = tk.Label(left, bg=c["qr_light"], bd=0, highlightthickness=0)
        self.qr_label.pack()
        ttk.Label(left, text="Наведите камеру айфона на код", style="Url.TLabel").pack(pady=(10, 0))
        self.pin_label = ttk.Label(left, text="", style="Pin.TLabel")
        self.pin_label.pack(pady=(6, 0))
        ttk.Label(left, text="код сопряжения", style="Url.TLabel").pack()
        self.url_label = ttk.Label(left, text="", style="Url.TLabel")
        self.url_label.pack(pady=(10, 0))

        # --- справа: статус, действия, настройки, журнал ---
        right = ttk.Frame(outer)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(3, weight=1)

        status = ttk.Frame(right, style="Panel.TFrame", padding=(14, 12))
        status.grid(row=0, column=0, sticky="ew")
        status.columnconfigure(1, weight=1)
        self.dot = tk.Canvas(status, width=12, height=12, bg=c["panel"], highlightthickness=0)
        self.dot.grid(row=0, column=0, padx=(0, 10))
        self.dot_id = self.dot.create_oval(1, 1, 11, 11, fill=c["sub"], outline="")
        self.status_label = ttk.Label(status, text="Запуск…", style="Panel.TLabel", font=(FONT, 11, "bold"))
        self.status_label.grid(row=0, column=1, sticky="w")
        self.detail_label = ttk.Label(status, text="", style="Sub.TLabel", wraplength=420, justify="left")
        self.detail_label.grid(row=1, column=1, sticky="w", pady=(2, 0))

        actions = ttk.Frame(right)
        actions.grid(row=1, column=0, sticky="ew", pady=12)
        self.pause_btn = ttk.Button(actions, text="Приостановить ввод", command=self.toggle_pause)
        self.pause_btn.pack(side="left")
        ttk.Button(actions, text="Новый код", command=self.new_pin).pack(side="left", padx=8)
        ttk.Button(actions, text="Копировать ссылку", command=self.copy_link).pack(side="left")

        settings = ttk.Frame(right, style="Panel.TFrame", padding=(14, 12))
        settings.grid(row=2, column=0, sticky="ew")
        settings.columnconfigure(1, weight=1)
        ttk.Label(settings, text="НАСТРОЙКИ", style="Head.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")

        ttk.Label(settings, text="Адрес ПК", style="Panel.TLabel").grid(row=1, column=0, sticky="w", pady=(10, 0))
        self.host_var = tk.StringVar()
        self.host_box = ttk.Combobox(settings, textvariable=self.host_var, state="readonly", width=26)
        self.host_box.grid(row=1, column=1, sticky="w", padx=10, pady=(10, 0))
        self.host_box.bind("<<ComboboxSelected>>", self._host_chosen)

        ttk.Label(settings, text="Порт", style="Panel.TLabel").grid(row=2, column=0, sticky="w", pady=(8, 0))
        port_row = ttk.Frame(settings, style="Panel.TFrame")
        port_row.grid(row=2, column=1, sticky="w", padx=10, pady=(8, 0))
        self.port_var = tk.StringVar(value=str(self.port))
        ttk.Entry(port_row, textvariable=self.port_var, width=8).pack(side="left")
        ttk.Button(port_row, text="Применить", command=self.apply_port).pack(side="left", padx=8)

        self.autostart_var = tk.BooleanVar(value=core.autostart_get() is not None)
        ttk.Checkbutton(settings, text="Запускать вместе с Windows", variable=self.autostart_var,
                        command=self.toggle_autostart).grid(row=3, column=0, columnspan=3, sticky="w", pady=(12, 0))
        self.minimized_var = tk.BooleanVar(value=bool(self.cfg["start_minimized"]))
        ttk.Checkbutton(settings, text="При автозапуске сразу сворачивать окно", variable=self.minimized_var,
                        command=self.toggle_minimized).grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.autostart_note = ttk.Label(settings, text="", style="Sub.TLabel", wraplength=420, justify="left")
        self.autostart_note.grid(row=5, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self._check_autostart_path()

        log_frame = ttk.Frame(right, style="Panel.TFrame", padding=(14, 10))
        log_frame.grid(row=3, column=0, sticky="nsew", pady=(12, 0))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(1, weight=1)
        ttk.Label(log_frame, text="ЖУРНАЛ", style="Head.TLabel").grid(row=0, column=0, sticky="w")
        self.log_text = tk.Text(log_frame, height=6, bg=c["panel"], fg=c["sub"], bd=0, highlightthickness=0,
                                font=("Consolas", 9), wrap="word", state="disabled", insertbackground=c["text"])
        self.log_text.grid(row=1, column=0, sticky="nsew", pady=(6, 0))

        if self.args.dry_run:
            r.title(TITLE + " — тестовый режим")
        r.after(0, self._dark_titlebar)

    def _dark_titlebar(self):
        if os.name != "nt":
            return
        try:
            self.root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            value = ctypes.c_int(1)
            for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (новые и старые сборки Windows 10)
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(value), 4) == 0:
                    break
        except Exception:
            pass

    def _append_log(self, line):
        t = self.log_text
        t.configure(state="normal")
        t.insert("end", line + "\n")
        lines = int(t.index("end-1c").split(".")[0])
        if lines > 300:
            t.delete("1.0", f"{lines - 300}.0")
        t.see("end")
        t.configure(state="disabled")

    def _set_state(self, state, detail):
        self.state = state
        self.state_detail = detail
        self._paint_status()

    def _paint_status(self):
        c = COLORS
        if self.state == "ok":
            if self.injector.paused:
                color, text = c["accent"], "Ввод приостановлен"
            elif self.phones:
                n = len(self.phones)
                color, text = c["ok"], f"Подключено телефонов: {n}"
            else:
                color, text = c["ok"], "Работает — ждём телефон"
            detail = ("Телефоны: " + ", ".join(self.phones)) if self.phones else \
                "Отсканируйте QR-код камерой айфона. Телефон и ПК должны быть в одной сети Wi-Fi."
            if self.args.dry_run:
                detail += "  Тестовый режим: клавиши не нажимаются."
        elif self.state == "wait":
            color, text, detail = c["accent"], "Ожидание сети", self.state_detail
        elif self.state == "bad":
            color, text, detail = c["bad"], "Не запущен", self.state_detail
        else:
            color, text, detail = c["sub"], "Запуск…", ""
        self.dot.itemconfigure(self.dot_id, fill=color)
        self.status_label.configure(text=text)
        self.detail_label.configure(text=detail)

    def _paint_qr(self):
        pin = self.server.pin
        self.pin_label.configure(text=f"{pin[:3]} {pin[3:]}")
        if self.qr_host:
            url = core.pairing_url(self.qr_host, self.port, pin)
            buf = io.BytesIO()
            qr = segno.make(url, error="m")
            scale = max(4, 236 // (qr.symbol_size(border=4)[0]))
            qr.save(buf, kind="png", scale=scale, border=4, dark=COLORS["qr_dark"], light=COLORS["qr_light"])
            self._qr_img = tk.PhotoImage(data=buf.getvalue())
            self.qr_label.configure(image=self._qr_img, text="", width=0, height=0)
            self.url_label.configure(text=f"http://{self.qr_host}:{self.port}")
        else:
            self._qr_img = None
            self.qr_label.configure(image="", text="Нет сети", fg=COLORS["sub"], bg=COLORS["panel"],
                                    width=24, height=12, font=(FONT, 11))
            self.url_label.configure(text="")
        if self.qr_host:
            self.qr_label.configure(bg=COLORS["qr_light"])

    def _fill_hosts(self, lan):
        values = ["Автоматически"] + lan
        current = self.cfg["host"]
        if current != "auto" and current not in lan:
            values.append(current)
        self.host_box.configure(values=values)
        self.host_var.set("Автоматически" if current == "auto" else current)

    # ---- действия ----
    def _host_chosen(self, _event=None):
        value = self.host_var.get()
        self.cfg["host"] = "auto" if value == "Автоматически" else value
        self.restart_server()

    def apply_port(self):
        try:
            port = int(self.port_var.get())
            if not 1024 <= port <= 65535:
                raise ValueError
        except ValueError:
            messagebox.showerror(TITLE, "Порт — число от 1024 до 65535.", parent=self.root)
            self.port_var.set(str(self.port))
            return
        if port == self.port and self.state == "ok":
            return
        self.port = port
        self.cfg["port"] = port
        self.log(f"порт изменён на {port}")
        self.restart_server()

    def toggle_pause(self):
        self.injector.paused = not self.injector.paused
        if self.injector.paused:
            self.hub.release_all()
        self.pause_btn.configure(text="Продолжить ввод" if self.injector.paused else "Приостановить ввод",
                                 style="Accent.TButton" if self.injector.paused else "TButton")
        self.log("ввод с телефона приостановлен" if self.injector.paused else "ввод с телефона возобновлён")
        self._paint_status()

    def new_pin(self):
        if not messagebox.askyesno(TITLE, "Сделать новый код сопряжения?\n\nВсе телефоны отключатся, "
                                          "и их нужно будет подключить заново по новому QR-коду.",
                                   parent=self.root):
            return
        pin = core.new_pin()
        self.cfg["pin"] = pin
        self.thread.set_pin(pin)
        self.server.pin = pin  # чтобы QR перерисовался сразу, не дожидаясь потока сервера
        self.log("создан новый код сопряжения")
        self._paint_qr()

    def copy_link(self):
        if not self.qr_host:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(core.pairing_url(self.qr_host, self.port, self.server.pin))
        self.log("ссылка для телефона скопирована")

    def toggle_autostart(self):
        want = self.autostart_var.get()
        try:
            core.autostart_set(want)
        except OSError as e:
            messagebox.showerror(TITLE, f"Не удалось изменить автозагрузку: {e}", parent=self.root)
        self.autostart_var.set(core.autostart_get() is not None)
        self.log("автозапуск включён" if self.autostart_var.get() else "автозапуск выключен")
        self._check_autostart_path()

    def toggle_minimized(self):
        self.cfg["start_minimized"] = bool(self.minimized_var.get())

    def _check_autostart_path(self):
        current = core.autostart_get()
        if current and current != core.autostart_command():
            self.autostart_note.configure(
                text="В автозагрузке записан другой путь к программе. Снимите и снова поставьте галочку, "
                     "чтобы запускалась эта копия.", foreground=COLORS["accent"])
        elif current and not core.FROZEN:
            self.autostart_note.configure(text="Сейчас запущен скрипт, а не exe: в автозагрузку записан Python.",
                                          foreground=COLORS["sub"])
        else:
            self.autostart_note.configure(text="")

    def quit(self):
        try:
            self.thread.shutdown()
        finally:
            self.hub.release_all()
            self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    parser = argparse.ArgumentParser(description="Web Numpad")
    parser.add_argument("--autostart", action="store_true", help="запуск из автозагрузки Windows")
    parser.add_argument("--port", type=int, help="порт на этот запуск (не сохраняется)")
    parser.add_argument("--config-dir", help="папка настроек (по умолчанию %%APPDATA%%\\WebNumpad)")
    parser.add_argument("--dry-run", action="store_true", help="тестовый режим: клавиши не нажимаются")
    parser.add_argument("--allow-second", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args.allow_second and not single_instance_or_focus():
        return
    windows_tweaks()
    try:
        App(args).run()
    except Exception as e:  # окно без консоли: показать ошибку, а не молча закрыться
        try:
            messagebox.showerror(TITLE, f"Web Numpad не запустился:\n\n{e}")
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
