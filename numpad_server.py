#!/usr/bin/env python3
"""Web Numpad — консольный вариант без окна. Обычно удобнее WebNumpad.exe (numpad_app.py).

Запуск: python numpad_server.py [--port 8790] [--dry-run] [--verbose]
"""
import argparse
import asyncio
import ctypes
import os
import sys
import time
from ctypes import wintypes

import numpad_core as core


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def install_exit_guards(hub):
    """Отпустить клавиши, если окно консоли закрыли крестиком или по Ctrl+C."""
    if os.name != "nt":
        return None
    handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

    @handler_type
    def on_console_event(_event):
        hub.release_all()
        return False

    ctypes.WinDLL("kernel32").SetConsoleCtrlHandler(on_console_event, True)
    return on_console_event  # ссылку нужно держать, иначе обработчик соберёт сборщик мусора


async def main_async(args):
    cfg = core.Config(args.config_dir)
    if args.new_pin:
        cfg["pin"] = core.new_pin()
    pin = args.pin or cfg["pin"]
    port = args.port or cfg["port"]

    injector = core.Injector(dry_run=args.dry_run, verbose=args.verbose, log=log)
    hub = core.Hub(injector)
    guard = install_exit_guards(hub)
    server = core.Server(pin, hub, log=log)

    hosts = [args.host] if args.host else core.lan_addresses()
    listeners = []
    for host in list(dict.fromkeys(hosts + ["127.0.0.1"])):
        try:
            listeners.append(await asyncio.start_server(server.handle, host, port))
        except OSError as e:
            print(f"Не удалось открыть {host}:{port} — {e}")
            continue
        server.allowed_hosts.add(f"{host}:{port}")
        if host == "127.0.0.1":
            server.allowed_hosts.add(f"localhost:{port}")
    if not listeners:
        return 1

    print()
    print("  Web Numpad запущен" + ("  (тестовый режим: клавиши НЕ нажимаются)" if args.dry_run else ""))
    print()
    for host in hosts:
        print(f"  Адрес для телефона:  {core.pairing_url(host, port, pin)}")
    if not hosts:
        print("  Адрес домашней сети не найден. Укажите его вручную: --host 192.168.x.x")
    print(f"  Код сопряжения:      {pin[:3]} {pin[3:]}")
    print()
    print("  Остановить: закройте это окно или нажмите Ctrl+C.")
    print()
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        hub.release_all()
        del guard


def main():
    parser = argparse.ArgumentParser(description="Web Numpad: цифровой блок на телефоне (консоль)")
    parser.add_argument("--port", type=int, help=f"порт (по умолчанию из настроек, {core.DEFAULT_PORT})")
    parser.add_argument("--host", help="адрес ПК в домашней сети (по умолчанию определяется сам)")
    parser.add_argument("--pin", help="код сопряжения на этот запуск (6 цифр)")
    parser.add_argument("--new-pin", action="store_true", help="сгенерировать и сохранить новый код")
    parser.add_argument("--config-dir", help="папка настроек (по умолчанию %%APPDATA%%\\WebNumpad)")
    parser.add_argument("--dry-run", action="store_true", help="не нажимать клавиши, только писать в лог")
    parser.add_argument("--verbose", action="store_true", help="писать в лог каждое нажатие")
    args = parser.parse_args()
    if sys.stdout and not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if args.pin and not (len(args.pin) == 6 and args.pin.isdigit()):
        parser.error("--pin: нужно ровно 6 цифр")
    try:
        sys.exit(asyncio.run(main_async(args)) or 0)
    except KeyboardInterrupt:
        print("\nОстановлен.")


if __name__ == "__main__":
    main()
