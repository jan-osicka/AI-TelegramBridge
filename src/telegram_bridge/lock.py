"""Single-instance lock. The OS lock decides, never the file's existence, so a
file left by a crashed process does not block the next start.

Windows: msvcrt.locking locks a byte range and blocks other processes from
reading it. The JSON info therefore lives at offset 0 and the locked byte is far
past EOF (LOCK_OFFSET), so read_info() works from another process while the
lock is held. POSIX: fcntl.flock on the whole fd.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

LOCK_OFFSET = 1 << 20
_WIN = sys.platform == "win32"
if _WIN:
    import msvcrt
else:
    import fcntl


class AlreadyRunning(Exception):
    def __init__(self, pid: int | None = None):
        super().__init__(f"already running (pid {pid})" if pid else "already running")
        self.pid = pid


def _try_lock(fd: int) -> bool:
    try:
        if _WIN:
            os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fd: int) -> None:
    try:
        if _WIN:
            os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


class LockHandle:
    def __init__(self, fd: int, path: Path):
        self._fd: int | None = fd
        self.path = path

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        _unlock(fd)
        os.close(fd)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()


def read_info(path: Path) -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _pid(info: dict | None) -> int | None:
    pid = (info or {}).get("pid")
    return pid if isinstance(pid, int) else None


def acquire(path: Path) -> LockHandle:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
    if not _try_lock(fd):
        os.close(fd)
        raise AlreadyRunning(_pid(read_info(path)))
    info = {"pid": os.getpid(), "started": datetime.now(timezone.utc).isoformat()}
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, json.dumps(info).encode())
    return LockHandle(fd, path)


def is_held(path: Path) -> bool:
    """True if some lock holder exists. Tries a non-blocking lock on a fresh fd
    and releases it at once. Also reports True for a lock held by this same
    process's LockHandle (a second fd conflicts on both flock and msvcrt)."""
    try:
        fd = os.open(path, os.O_RDWR | getattr(os, "O_BINARY", 0))
    except OSError:
        return False
    try:
        if _try_lock(fd):
            _unlock(fd)
            return False
        return True
    finally:
        os.close(fd)
