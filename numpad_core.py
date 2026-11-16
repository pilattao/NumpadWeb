"""Web Numpad — ядро: нажатие клавиш, WebSocket-сервер, настройки.

Используется и консольным сервером (numpad_server.py), и окном (numpad_app.py).
Только стандартная библиотека Python 3.9+.
"""
import asyncio
import base64
import ctypes
import hashlib
import hmac
import ipaddress
import itertools
import json
import os
import re
import secrets
import socket
import struct
import sys
import threading
import time
import zlib
from ctypes import wintypes
from urllib.parse import unquote, urlparse

APP_NAME = "Web Numpad"
DEFAULT_PORT = 8790
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# В собранном exe файлы страницы лежат во временной папке PyInstaller,
# а настройки — в %APPDATA%, чтобы код сопряжения не менялся при каждом запуске.
FROZEN = getattr(sys, "frozen", False)
SOURCE_DIR = os.path.dirname(os.path.abspath(sys.executable if FROZEN else __file__))
RESOURCE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def default_config_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "WebNumpad")


# ---------------------------------------------------------------------------
# Клавиши. Сервер умеет нажимать только то, что есть в этой таблице.
# Значение: (скан-код set 1, расширенная клавиша с префиксом E0).
# ---------------------------------------------------------------------------
KEYS = {
    "numlock": (0x45, False),
    "div": (0x35, True),
    "mul": (0x37, False),
    "sub": (0x4A, False),
    "add": (0x4E, False),
    "n7": (0x47, False), "n8": (0x48, False), "n9": (0x49, False),
    "n4": (0x4B, False), "n5": (0x4C, False), "n6": (0x4D, False),
    "n1": (0x4F, False), "n2": (0x50, False), "n3": (0x51, False),
    "n0": (0x52, False),
    "dot": (0x53, False),
    "enter": (0x1C, True),
    "back": (0x0E, False),
}
NO_REPEAT = {"numlock"}

# ---------------------------------------------------------------------------
# Нажатие клавиш. Весь способ ввода — в классе Injector, чтобы его можно было
# заменить другим (например, драйвером Interception), не трогая остальное.
# ---------------------------------------------------------------------------
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
VK_NUMLOCK = 0x90


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_void_p)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_void_p)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


class Injector:
    """SendInput со скан-кодом: уровень, на котором клавиатуру читают игры (DirectInput)."""

    def __init__(self, dry_run=False, verbose=False, log=None):
        self.dry_run = dry_run
        self.verbose = verbose
        self.paused = False
        self.log = log or (lambda msg: None)
        self.user32 = None
        if os.name == "nt":
            if ctypes.sizeof(_INPUT) != 40:
                raise RuntimeError("Неверный размер структуры INPUT: нужен 64-битный Python")
            self.user32 = ctypes.WinDLL("user32", use_last_error=True)
            self.user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int)
            self.user32.SendInput.restype = wintypes.UINT
            self.user32.GetKeyState.argtypes = (ctypes.c_int,)
            self.user32.GetKeyState.restype = ctypes.c_short
        elif not dry_run:
            raise RuntimeError("Нажатие клавиш работает только в Windows (или включите тестовый режим)")

    def send(self, key, up):
        scan, ext = KEYS[key]
        if self.verbose or self.dry_run:
            self.log(f"{'отпущена' if up else 'нажата  '} {key:<8} scan=0x{scan:02X}{' E0' if ext else ''}"
                     f"{'  [тест]' if self.dry_run else ''}")
        if self.dry_run:
            return True
        inp = _INPUT()
        inp.type = 1  # INPUT_KEYBOARD
        inp.u.ki.wScan = scan
        inp.u.ki.dwFlags = KEYEVENTF_SCANCODE | (KEYEVENTF_EXTENDEDKEY if ext else 0) | (KEYEVENTF_KEYUP if up else 0)
        return self.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT)) == 1

    def numlock_on(self):
        if not self.user32:
            return False
        return bool(self.user32.GetKeyState(VK_NUMLOCK) & 1)


class Hub:
    """Кто какую клавишу держит. Клавиша отпускается, когда её отпустил последний телефон."""

    def __init__(self, injector):
        self.injector = injector
        self.holders = {k: set() for k in KEYS}
        self.lock = threading.Lock()

    def down(self, sid, key):
        with self.lock:
            if self.injector.paused:
                return True
            first = not self.holders[key]
            self.holders[key].add(sid)
            return self.injector.send(key, False) if first else True

    def up(self, sid, key):
        with self.lock:
            if sid not in self.holders[key]:
                return True
            self.holders[key].discard(sid)
            return self.injector.send(key, True) if not self.holders[key] else True

    def release_session(self, sid):
        released = []
        for key in KEYS:
            if sid in self.holders[key]:
                self.up(sid, key)
                released.append(key)
        return released

    def release_all(self):
        with self.lock:
            for key, who in self.holders.items():
                if who:
                    who.clear()
                    self.injector.send(key, True)


# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------
class Config:
    DEFAULTS = {"pin": "", "port": DEFAULT_PORT, "host": "auto", "start_minimized": True}

    def __init__(self, folder=None):
        self.folder = folder or default_config_dir()
        self.path = os.path.join(self.folder, "config.json")
        self.data = dict(self.DEFAULTS)
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                self.data.update(json.load(f))
        except (OSError, ValueError):
            pass
        pin = str(self.data.get("pin", ""))
        if not (len(pin) == 6 and pin.isdigit()):
            self.data["pin"] = self._legacy_pin() or new_pin()
            self.save()

    def _legacy_pin(self):
        """Код из консольной версии, чтобы уже сопряжённый телефон не пришлось сопрягать заново."""
        for folder in (SOURCE_DIR, os.path.dirname(os.path.abspath(__file__))):
            try:
                with open(os.path.join(folder, "numpad_config.json"), encoding="utf-8") as f:
                    pin = str(json.load(f).get("pin", ""))
                if len(pin) == 6 and pin.isdigit():
                    return pin
            except (OSError, ValueError):
                continue
        return None

    def save(self):
        os.makedirs(self.folder, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def __getitem__(self, key):
        return self.data.get(key, self.DEFAULTS.get(key))

    def __setitem__(self, key, value):
        self.data[key] = value
        self.save()


def new_pin():
    return f"{secrets.randbelow(1_000_000):06d}"


# ---------------------------------------------------------------------------
# Сеть
# ---------------------------------------------------------------------------
_PRIVATE_NETS = [ipaddress.ip_network(n) for n in ("192.168.0.0/16", "10.0.0.0/8", "172.16.0.0/12")]


def lan_addresses():
    """Адреса домашней сети. VPN-туннели (198.18.x.x) и Tailscale (100.64.x.x) сюда не попадают."""
    found = []
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        infos = []
    for info in infos:
        ip = info[4][0]
        if ip not in found:
            found.append(ip)
    ranked = []
    for ip in found:
        addr = ipaddress.ip_address(ip)
        for rank, net in enumerate(_PRIVATE_NETS):
            if addr in net:
                ranked.append((rank, ip))
    return [ip for _, ip in sorted(ranked)]


def pairing_url(host, port, pin):
    return f"http://{host}:{port}/#pin={pin}"


# ---------------------------------------------------------------------------
# Иконка (PNG и ICO без сторонних библиотек)
# ---------------------------------------------------------------------------
def make_icon(size=180):
    """Тёмный фон и сетка клавиш, центральная янтарная."""
    bg, key, accent = (14, 15, 19), (48, 52, 62), (255, 178, 36)
    pad, gap = size // 6, max(1, size // 22)
    cell = (size - 2 * pad - 2 * gap) // 3
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            color = bg
            cx, cy = x - pad, y - pad
            if cx >= 0 and cy >= 0:
                ix, rx = divmod(cx, cell + gap)
                iy, ry = divmod(cy, cell + gap)
                if ix < 3 and iy < 3 and rx < cell and ry < cell:
                    color = accent if (ix, iy) == (1, 1) else key
            row += bytes(color)
        rows.append(bytes(row))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


def make_ico(sizes=(16, 24, 32, 48, 64, 128, 256)):
    images = [make_icon(s) for s in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for s, png in zip(sizes, images):
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(png), offset)
        offset += len(png)
        blobs += png
    return header + entries + blobs


MANIFEST = json.dumps({
    "name": APP_NAME, "short_name": "Numpad", "display": "standalone", "orientation": "portrait",
    "background_color": "#0e0f13", "theme_color": "#0e0f13",
    "icons": [{"src": "/icon.png", "sizes": "180x180", "type": "image/png"}],
})


# ---------------------------------------------------------------------------
# Сервер: HTTP для страницы и WebSocket для нажатий
# ---------------------------------------------------------------------------
class ProtocolError(Exception):
    pass


async def read_frame(reader):
    head = await reader.readexactly(2)
    opcode = head[0] & 0x0F
    masked = head[1] & 0x80
    length = head[1] & 0x7F
    if length == 126:
        length = struct.unpack(">H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", await reader.readexactly(8))[0]
    if length > 4096 or not masked:
        raise ProtocolError("bad frame")
    mask = await reader.readexactly(4)
    data = bytearray(await reader.readexactly(length))
    for i in range(length):
        data[i] ^= mask[i & 3]
    return opcode, bytes(data)


def encode_frame(payload, opcode=1):
    if len(payload) < 126:
        return bytes([0x80 | opcode, len(payload)]) + payload
    return bytes([0x80 | opcode, 126]) + struct.pack(">H", len(payload)) + payload


class Server:
    MAX_FAILS = 5
    LOCK_SECONDS = 60
    HEARTBEAT_TIMEOUT = 2.5
    REPEAT_DELAY = 0.4
    REPEAT_INTERVAL = 0.033

    def __init__(self, pin, hub, log=None, on_event=None):
        self.pin = pin
        self.hub = hub
        self.log = log or (lambda msg: None)
        self.on_event = on_event or (lambda kind, **data: None)
        self.allowed_hosts = set()
        self.sid_counter = itertools.count(1)
        self.fails = 0
        self.locked_until = 0.0
        self.icon = make_icon()
        self.sessions = {}  # sid -> {"peer", "writer", "authed"}

    def set_pin(self, pin):
        """Новый код: все телефоны отключаются и должны сопрягаться заново."""
        self.pin = pin
        self.fails = 0
        self.locked_until = 0.0
        self.drop_all()

    def drop_all(self):
        for info in list(self.sessions.values()):
            try:
                info["writer"].close()
            except Exception:
                pass

    def phones(self):
        return sorted({s["peer"] for s in self.sessions.values() if s["authed"]})

    def _changed(self):
        self.on_event("phones", phones=self.phones())

    # ---- HTTP ----
    async def handle(self, reader, writer):
        peer = (writer.get_extra_info("peername") or ("?", 0))[0]
        try:
            try:
                raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
                return
            lines = raw.decode("latin-1").split("\r\n")
            parts = lines[0].split(" ")
            if len(parts) < 2 or parts[0] != "GET":
                return await self.respond(writer, 405, "text/plain", b"GET only")
            # Айфон (или VPN-приложение на нём) бывает присылает в запросе и «#pin=…», сырым или как «%23»,
            # а через прокси — полный адрес «http://ПК:порт/…». Отрезаем всё это и смотрим только путь.
            target = parts[1]
            if "://" in target:
                target = urlparse(target).path or "/"
            path = unquote(target).split("#")[0].split("?")[0] or "/"
            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    name, value = line.split(":", 1)
                    headers[name.strip().lower()] = value.strip()
            # Чужие сайты и подмена адреса не должны достучаться до сервера.
            if headers.get("host", "").lower() not in self.allowed_hosts:
                return await self.respond(writer, 403, "text/plain", b"bad host")
            if headers.get("upgrade", "").lower() == "websocket" and path == "/ws":
                origin = headers.get("origin")
                if not origin or urlparse(origin).netloc.lower() != headers["host"].lower():
                    return await self.respond(writer, 403, "text/plain", b"bad origin")
                key = headers.get("sec-websocket-key")
                if not key:
                    return await self.respond(writer, 400, "text/plain", b"no key")
                accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
                writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                              f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
                await writer.drain()
                return await self.session(reader, writer, peer)
            if path in ("/", "/index.html"):
                with open(os.path.join(RESOURCE_DIR, "index.html"), "rb") as f:
                    return await self.respond(writer, 200, "text/html; charset=utf-8", f.read())
            if path == "/manifest.webmanifest":
                return await self.respond(writer, 200, "application/manifest+json", MANIFEST.encode())
            if path in ("/icon.png", "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png", "/favicon.ico"):
                return await self.respond(writer, 200, "image/png", self.icon)
            # Любой другой адрес — на главную (путь уже не «/», так что петли не будет).
            self.log(f"запрос {re.sub(r'[0-9]', '•', unquote(parts[1]))[:80]} от {peer} — перенаправляю на главную")
            await self.redirect(writer, "/")
        except (ConnectionError, OSError):
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def respond(self, writer, status, ctype, body):
        reason = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed"}[status]
        writer.write((f"HTTP/1.1 {status} {reason}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
                      "Cache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\nConnection: close\r\n\r\n").encode() + body)
        await writer.drain()

    async def redirect(self, writer, location):
        writer.write((f"HTTP/1.1 302 Found\r\nLocation: {location}\r\nContent-Length: 0\r\n"
                      "Cache-Control: no-store\r\nConnection: close\r\n\r\n").encode())
        await writer.drain()

    # ---- WebSocket ----
    async def session(self, reader, writer, peer):
        sid = next(self.sid_counter)
        info = {"peer": peer, "writer": writer, "authed": False}
        self.sessions[sid] = info
        state = {"repeat": False, "last_seen": time.monotonic(), "unauth_msgs": 0,
                 "repeat_task": None, "repeat_key": None}

        def send(obj):
            try:
                writer.write(encode_frame(json.dumps(obj).encode()))
            except Exception:
                pass

        def stop_repeat():
            task = state["repeat_task"]
            if task:
                task.cancel()
            state["repeat_task"] = None
            state["repeat_key"] = None

        async def repeat(key):
            await asyncio.sleep(self.REPEAT_DELAY)
            while sid in self.hub.holders[key]:
                self.hub.injector.send(key, False)
                await asyncio.sleep(self.REPEAT_INTERVAL)

        async def watchdog():
            while True:
                await asyncio.sleep(0.5)
                if time.monotonic() - state["last_seen"] > self.HEARTBEAT_TIMEOUT:
                    stop_repeat()
                    released = self.hub.release_session(sid)
                    if released:
                        self.log(f"телефон {peer} молчит — отпустил зажатые клавиши: {', '.join(released)}")
                        send({"t": "released"})

        guard = asyncio.ensure_future(watchdog())
        try:
            while True:
                opcode, data = await read_frame(reader)
                state["last_seen"] = time.monotonic()
                if opcode == 8:
                    break
                if opcode == 9:
                    writer.write(encode_frame(data, 10))
                    continue
                if opcode != 1:
                    continue
                try:
                    msg = json.loads(data)
                    kind = msg.get("t")
                except (ValueError, AttributeError):
                    raise ProtocolError("bad json")

                if not info["authed"]:
                    state["unauth_msgs"] += 1
                    if state["unauth_msgs"] > 20:
                        break
                    if kind != "auth":
                        continue
                    now = time.monotonic()
                    if now < self.locked_until:
                        send({"t": "auth", "ok": False, "locked": int(self.locked_until - now) + 1})
                        continue
                    if hmac.compare_digest(str(msg.get("pin", "")), self.pin):
                        info["authed"] = True
                        self.fails = 0
                        self.log(f"телефон {peer} подключён")
                        send({"t": "auth", "ok": True, "pin": self.pin, "numlock": self.hub.injector.numlock_on(),
                              "dry": self.hub.injector.dry_run})
                        self._changed()
                    else:
                        self.fails += 1
                        if self.fails >= self.MAX_FAILS:
                            self.fails = 0
                            self.locked_until = now + self.LOCK_SECONDS
                            self.log(f"5 неверных кодов подряд — ввод кода закрыт на {self.LOCK_SECONDS} с")
                            send({"t": "auth", "ok": False, "locked": self.LOCK_SECONDS})
                        else:
                            self.log(f"неверный код от {peer}")
                            send({"t": "auth", "ok": False})
                    continue

                if kind == "p":
                    send({"t": "q", "ts": msg.get("ts"), "numlock": self.hub.injector.numlock_on(),
                          "paused": self.hub.injector.paused})
                elif kind in ("d", "u"):
                    key = msg.get("k")
                    if key not in KEYS:
                        continue
                    if kind == "d":
                        ok = self.hub.down(sid, key)
                        if state["repeat"] and key not in NO_REPEAT and not self.hub.injector.paused:
                            stop_repeat()
                            state["repeat_key"] = key
                            state["repeat_task"] = asyncio.ensure_future(repeat(key))
                    else:
                        if state["repeat_key"] == key:
                            stop_repeat()
                        ok = self.hub.up(sid, key)
                    if not ok:
                        send({"t": "err", "msg": "blocked"})
                elif kind == "allup":
                    stop_repeat()
                    self.hub.release_session(sid)
                elif kind == "cfg":
                    state["repeat"] = bool(msg.get("repeat"))
                    if not state["repeat"]:
                        stop_repeat()
        except (asyncio.IncompleteReadError, ConnectionError, OSError, ProtocolError):
            pass
        finally:
            guard.cancel()
            stop_repeat()
            released = self.hub.release_session(sid)
            was_authed = info["authed"]
            self.sessions.pop(sid, None)
            try:
                writer.close()
            except Exception:
                pass
            if was_authed:
                self.log(f"телефон {peer} отключился" + (f" — отпустил: {', '.join(released)}" if released else ""))
                self._changed()


# ---------------------------------------------------------------------------
# Сервер в отдельном потоке — для окна
# ---------------------------------------------------------------------------
class ServerThread:
    """asyncio-цикл в фоне. Методы можно вызывать из потока окна."""

    def __init__(self, server):
        self.server = server
        self.loop = asyncio.new_event_loop()
        self.listeners = []
        self.bound = []  # адреса, на которых реально слушаем
        self.thread = threading.Thread(target=self._run, name="numpad-server", daemon=True)
        self.thread.start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _call(self, coro, timeout=10):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    async def _stop(self):
        for listener in self.listeners:
            listener.close()
        for listener in self.listeners:
            try:
                await asyncio.wait_for(listener.wait_closed(), 2)
            except Exception:
                pass
        self.listeners = []
        self.bound = []
        self.server.drop_all()
        self.server.hub.release_all()

    async def _start(self, hosts, port):
        await self._stop()
        errors = []
        allowed = set()
        for host in list(dict.fromkeys(list(hosts) + ["127.0.0.1"])):
            try:
                self.listeners.append(await asyncio.start_server(self.server.handle, host, port))
            except OSError as e:
                errors.append((host, e))
                continue
            self.bound.append(host)
            allowed.add(f"{host}:{port}")
            if host == "127.0.0.1":
                allowed.add(f"localhost:{port}")
        self.server.allowed_hosts = allowed
        return list(self.bound), errors

    def start(self, hosts, port):
        return self._call(self._start(hosts, port))

    def stop(self):
        try:
            self._call(self._stop(), timeout=5)
        except Exception:
            self.server.hub.release_all()

    def set_pin(self, pin):
        self.loop.call_soon_threadsafe(self.server.set_pin, pin)

    def shutdown(self):
        self.stop()
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(3)


# ---------------------------------------------------------------------------
# Автозагрузка (реестр текущего пользователя, без прав администратора)
# ---------------------------------------------------------------------------
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "WebNumpad"


def autostart_command():
    if FROZEN:
        return f'"{sys.executable}" --autostart'
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    exe = pythonw if os.path.exists(pythonw) else sys.executable
    return f'"{exe}" "{os.path.join(os.path.dirname(os.path.abspath(__file__)), "numpad_app.py")}" --autostart'


def autostart_get(value_name=RUN_VALUE):
    if os.name != "nt":
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return winreg.QueryValueEx(key, value_name)[0]
    except OSError:
        return None


def autostart_set(enabled, value_name=RUN_VALUE):
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, value_name, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(key, value_name)
            except FileNotFoundError:
                pass
