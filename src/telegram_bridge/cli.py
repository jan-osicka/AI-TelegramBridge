import argparse
import asyncio
import sys


def _run(args) -> int:
    from . import bridge, lock, settings
    try:
        handle = lock.acquire(settings.LOCK)
    except lock.AlreadyRunning as e:
        print(f"telegram-bridge is already running (pid {e.pid if e.pid else '?'}).")
        return 0 if args.service else 3
    try:
        asyncio.run(bridge.main())
    except KeyboardInterrupt:
        pass
    finally:
        handle.release()
    return 0


def _notify(args) -> int:
    from . import notify
    return notify.run(args)


def _doctor(args) -> int:
    from . import doctor
    return doctor.run(args)


def _setup(args) -> int:
    from . import setupcmd
    return setupcmd.run(args)


def _service(args) -> int:
    from . import service
    return service.run(args)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="telegram-bridge")
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="run the bridge")
    r.add_argument("--service", action="store_true", help="run as a background service")
    r.set_defaults(func=_run)
    n = sub.add_parser("notify", help="send a message (and files) to the owner on Telegram")
    n.add_argument("text", nargs="?", help="message text (default: read stdin)")
    n.add_argument("--bot", help="bot name from config (default: the first bot)")
    n.add_argument("--file", action="append", default=[], metavar="PATH", help="file to attach (repeatable); a refused or failed upload gives exit 5")
    n.add_argument("--silent", action="store_true", help="no notification sound")
    n.set_defaults(func=_notify)
    d = sub.add_parser("doctor", help="check the installation (read-only)")
    d.add_argument("--online", action="store_true", help="also call Telegram getMe and the local model")
    d.set_defaults(func=_doctor)
    s = sub.add_parser("setup", help="create the home folder, .env template, TOTP secret and config")
    s.add_argument("--whisper", action="store_true", help="also download the Whisper model")
    s.set_defaults(func=_setup)
    sv = sub.add_parser("service", help="install, remove or print the autostart entry")
    sv.add_argument("action", choices=["install", "remove", "print"])
    sv.set_defaults(func=_service)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
