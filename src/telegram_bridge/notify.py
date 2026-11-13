"""`telegram-bridge notify`: send a message and files to the owner from a script.

Uses the Bot API directly (no polling, no lock), so it works while the bridge runs or is locked.
Same redaction, HTML rendering and file allow-list as chat replies.

Exit codes: 0 ok; 2 usage/config; 4 Telegram/network/unexpected failure; 5 an attached file was not
delivered (refused by the allow-list, or its upload failed). The text is still sent in that case.
"""
import asyncio
import os
import sys
import time

from telegram import Bot
from telegram.error import BadRequest, TelegramError

from . import fmt, outfiles, redaction, settings

bot_factory = Bot   # replaced in tests; called with the token, used as an async context manager


def _fail(msg: str, code: int, known=()) -> int:
    msg, _ = redaction.redact(" ".join(str(msg).split()), known)
    line = f"notify: {msg}" + chr(10)
    enc = getattr(sys.stderr, "encoding", None) or "utf-8"
    if sys.stderr is not None:
        sys.stderr.write(line.encode(enc, "backslashreplace").decode(enc))
    return code


def _log(ids, length, nfiles, code):
    try:
        settings.LOGS.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(settings.LOGS / "notify.log", "a", encoding="utf-8") as f:
            f.write(f"{ts} to={','.join(map(str, ids))} len={length} files={nfiles} exit={code}\n")
    except OSError:
        pass


async def _send_html(bot, chat, html, plain, silent):
    kw = {"disable_notification": True} if silent else {}
    try:
        await bot.send_message(chat, html, parse_mode="HTML", **kw)
    except BadRequest:
        await bot.send_message(chat, plain, **kw)


async def send_notification(token, user_ids, text, files, silent, bot_factory=None,
                            allowed=None, known=()) -> int:
    """Send text (Markdown, redacted) and files to every id. Returns the exit code: 0, 4 or 5."""
    factory = bot_factory or globals()["bot_factory"]
    text, _ = redaction.redact(text or "", known)
    allowed = allowed if allowed is not None else [settings.OUTBOX]
    refused = []
    try:
        async with factory(token) as bot:
            for uid in user_ids:
                for html, plain in fmt.render_chunks(text):
                    await _send_html(bot, uid, html, plain, silent)
                if files:
                    for n in await outfiles.send(bot, uid, [(f, None) for f in files], allowed, []):
                        if n not in refused:
                            refused.append(n)
    except TelegramError as e:
        return _fail(f"Telegram request failed ({type(e).__name__}): {e}", 4, [token, *known])
    except OSError as e:
        return _fail(f"network error ({type(e).__name__})", 4, [token, *known])
    except Exception as e:
        return _fail(f"unexpected error ({type(e).__name__}): {e}", 4, [token, *known])
    for note in refused:
        _fail("file not sent: " + note.removeprefix(outfiles.note_prefix()), 5)
    return 5 if refused else 0


def _read_stdin() -> str:
    buf = getattr(sys.stdin, "buffer", None)
    if buf is None:
        return sys.stdin.read().lstrip(chr(0xFEFF))
    return buf.read().decode("utf-8", "replace").lstrip(chr(0xFEFF))


def run(args) -> int:
    text = args.text
    if text is None and sys.stdin is not None and not sys.stdin.isatty():
        text = _read_stdin()
    text = (text or "").strip()
    files = [os.path.abspath(f) for f in args.file]
    if not text and not files:
        return _fail("nothing to send (give TEXT, stdin or --file)", 2)
    try:
        cfg = settings.load_config()
    except (OSError, ValueError) as e:
        return _fail(f"cannot read config: {type(e).__name__}", 2)
    bots = cfg.get("bots") or {}
    name = args.bot or next(iter(bots), None)
    if name not in bots:
        return _fail(f"unknown bot {args.bot!r}" if args.bot else "no bots in config", 2)
    env, ids = settings.load_secrets()
    known = redaction.secret_values(env)
    redaction.set_known(known)
    token = env.get(bots[name].get("token_env", ""), "")
    if not token:
        return _fail(f"token for bot {name!r} is missing in .env", 2)
    if not ids:
        return _fail("ALLOWED_USER_IDS is empty in .env", 2)
    allowed = outfiles.allowed_dirs(cfg["general"])
    code = asyncio.run(send_notification(token, ids, text, files, args.silent, allowed=allowed, known=known))
    _log(ids, len(text), len(files), code)
    return code
