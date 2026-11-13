"""Periodic housekeeping: inbox cleanup and cwd allow-list."""
import asyncio
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("bridge.housekeeping")


def cleanup_inbox(inbox, retention_days, now=None):
    """Delete files older than retention_days (0 = keep everything). Returns the number deleted."""
    inbox = Path(inbox)
    if retention_days <= 0 or not inbox.is_dir():
        return 0
    cutoff = (time.time() if now is None else now) - retention_days * 86400
    n = 0
    for p in inbox.iterdir():
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
                n += 1
        except OSError:
            log.warning("could not delete old inbox file %s", p.name)
    if n:
        log.info("inbox cleanup: deleted %d file(s) older than %s day(s)", n, retention_days)
    return n


async def inbox_loop(inbox, retention_days, interval_s=24 * 3600):
    while True:
        try:
            cleanup_inbox(inbox, retention_days)
        except Exception:
            log.exception("inbox cleanup failed")
        await asyncio.sleep(interval_s)


def _norm(p):
    return os.path.normcase(os.path.normpath(str(Path(p).resolve())))


def cwd_allowed(path, roots):
    """True if the resolved path is one of the roots or inside one (case-insensitive on Windows)."""
    try:
        target = _norm(path)
    except (OSError, ValueError):
        return False
    for r in roots:
        root = _norm(r)
        if target == root or target.startswith(root.rstrip("/\\") + os.sep):
            return True
    return False
