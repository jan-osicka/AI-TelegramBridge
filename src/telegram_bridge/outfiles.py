"""Files the agent sends to the user: '[tg-file] <path>' lines in its final reply.

extract() pulls the marker lines out of the text, check() decides whether a path may leave the
computer, send() uploads the allowed ones. send() only needs a telegram Bot-like object, so the
notify CLI can reuse it.
"""
import logging
import os
import re
from pathlib import Path

from telegram import ReplyParameters

from . import redaction, settings

log = logging.getLogger(__name__)

MARKER = "[tg-file]"
MAX_FILES = 10
MAX_BYTES = 50 * 1024 * 1024
PHOTO_MAX_BYTES = 10 * 1024 * 1024
CAPTION_MAX = 1024
PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXT = {".mp4", ".mov"}

CONVENTION = ("To send a file to the user, put it in an allowed folder and add a line "
              "'[tg-file] <absolute path>' to your final reply (one line per file, max 10). "
              "Default folder: {outbox}.")


def convention_sentence(outbox) -> str:
    return CONVENTION.replace("{outbox}", str(outbox))


def extract(text: str, marker: str = MARKER):
    """Return (text without marker lines, [(raw path, caption)]); at most 10 files are kept and
    the extras are named in a note line at the end of the text."""
    pat = re.compile(r"^[ \t]*" + re.escape(marker) + r"[ \t]+(.+?)[ \t]*$")
    kept, files, extra = [], [], []
    fence = None
    for line in text.splitlines():
        fm = re.match(r"[ 	]*(`{3,}|~{3,})", line)
        if fm:
            if fence is None:
                fence = fm.group(1)[0]
            elif fm.group(1)[0] == fence:
                fence = None
        m = None if (fence or fm) else pat.match(line)
        if not m:
            kept.append(line)
            continue
        rest = m.group(1)
        q = re.match(r'"([^"]+)"[ \t]*(?:\|[ \t]*(.*))?$', rest)
        item = (q.group(1), (q.group(2) or "").strip() or None) if q else (rest.strip('"'), None)
        (files if len(files) < MAX_FILES else extra).append(item)
    out = "\n".join(kept).strip()
    notes = [f"{note_prefix()}{_name(p)} (over the limit of {MAX_FILES} files)" for p, _ in extra]
    if notes:
        out = (out + "\n" if out else "") + "\n".join(notes)
    return out, files


def note_prefix() -> str:
    return "📎 not sent: "


def _name(raw: str) -> str:
    return re.split(r"[\\/]", raw.rstrip("\\/"))[-1] or raw


def _norm(p: Path) -> str:
    return os.path.normcase(str(p.resolve()))


def _inside(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder.rstrip("\\/") + os.sep)


def allowed_dirs(general: dict) -> list[Path]:
    """The outbox plus the extra folders from config `outbox_dirs`."""
    extra = [Path(d).expanduser() for d in (general.get("outbox_dirs") or [])]
    return [settings.OUTBOX, *extra]


def always_denied() -> list[Path]:
    import telegram_bridge
    dirs = [settings.home(), Path(telegram_bridge.__file__).parent, settings.CODE_DIR]
    pkg_root = Path(telegram_bridge.__file__).resolve().parent.parent.parent
    if (pkg_root / "pyproject.toml").exists() and Path(telegram_bridge.__file__).resolve().parent.parent.name == "src":
        dirs.append(pkg_root)
    return dirs


def check(raw: str, allowed: list[Path], denied: list[Path], max_bytes: int = MAX_BYTES,
          base: Path | None = None):
    """Return (resolved path, None) or (None, reason)."""
    try:
        p = Path(raw).expanduser()
        if base is not None and not p.is_absolute():
            p = Path(base) / p
        p = p.resolve()
    except (OSError, ValueError, RuntimeError):
        return None, "not found"
    n = _norm(p)
    parts = [os.path.normcase(x) for x in p.parts]
    if p.name.lower().startswith(".env"):
        return None, "blocked file"
    if ".git" in parts:
        return None, "blocked folder"
    outbox = _norm(settings.OUTBOX)
    for d in [*denied, *always_denied()]:
        dn = _norm(Path(d))
        if _inside(n, dn) and not (_inside(n, outbox) and dn == _norm(settings.home())):
            return None, "blocked folder"
    if not any(_inside(n, _norm(Path(a))) for a in allowed):
        return None, "outside allowed folders"
    if not p.is_file():
        return None, "not found"
    try:
        size = p.stat().st_size
    except OSError:
        return None, "not found"
    if size > max_bytes:
        return None, f"too large (>{max_bytes // (1024 * 1024)} MB)"
    return p, None


def kind(path) -> str:
    p = Path(path)
    ext = p.suffix.lower()
    if ext in PHOTO_EXT:
        try:
            if p.stat().st_size <= PHOTO_MAX_BYTES:
                return "photo"
        except OSError:
            pass
        return "document"
    return "video" if ext in VIDEO_EXT else "document"


async def send(bot, chat_id, items, allowed, denied, reply_to=None, base: Path | None = None) -> list[str]:
    """Upload each (raw path, caption) that passes check(); return one note per file not sent."""
    notes = []
    kw = {"reply_parameters": ReplyParameters(reply_to, allow_sending_without_reply=True)} if reply_to else {}
    for raw, caption in items:
        path, why = check(raw, allowed, denied, base=base)
        if path is None:
            notes.append(f"{note_prefix()}{_name(raw)} ({why})")
            continue
        cap = redaction.redact(caption)[0][:CAPTION_MAX] if caption else None
        k = kind(path)
        method = getattr(bot, "send_" + k)
        try:
            with open(path, "rb") as f:
                await method(chat_id, f, **({"filename": path.name} if k == "document" else {}),
                             **({"caption": cap} if cap else {}), **kw)
        except Exception as e:
            log.warning("sending file failed: %s", type(e).__name__)
            notes.append(f"{note_prefix()}{path.name} (upload failed)")
    return notes
