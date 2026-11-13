"""Cross-platform process and permission helpers."""
import asyncio
import getpass
import os
import signal
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
NEW_GROUP = 0x00000200
NO_WINDOW = 0x08000000


def spawn_kwargs():
    """Extra kwargs for subprocess spawns: hidden window on Windows, own session on POSIX."""
    if IS_WINDOWS:
        return {"creationflags": NO_WINDOW}
    return {"start_new_session": True}


def kill_tree(pid):
    """Ask a process tree to stop; never blocks. Windows: taskkill /F. POSIX: SIGTERM to the group.
    Quiet if the process is already gone. Follow up with escalate() when the caller can await."""
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                       capture_output=True, creationflags=NO_WINDOW)
        return
    _killpg(pid, signal.SIGTERM)


def force_kill_tree(pid):
    """POSIX: SIGKILL to the group. Windows: kill_tree is already forceful."""
    if IS_WINDOWS:
        kill_tree(pid)
    else:
        _killpg(pid, getattr(signal, "SIGKILL", 9))


def _killpg(pid, sig):
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


async def escalate(proc, grace=5.0):
    """After kill_tree: wait for the child (this lets the event loop reap it), SIGKILL after grace."""
    if proc.returncode is not None:
        return
    try:
        await asyncio.wait_for(proc.wait(), grace)
    except asyncio.TimeoutError:
        force_kill_tree(proc.pid)
        await proc.wait()


async def terminate_tree(proc, grace=5.0):
    kill_tree(proc.pid)
    await escalate(proc, grace)


def launch_detached(cmd, log_path=None):
    """Start cmd hidden, in its own process group, not tied to the bridge (survives bridge exit).
    (DETACHED_PROCESS was tried on Windows and made the launch fail silently.)"""
    out = open(log_path, "ab") if log_path else subprocess.DEVNULL
    kw = {"creationflags": NEW_GROUP | NO_WINDOW} if IS_WINDOWS else {"start_new_session": True}
    try:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                         close_fds=True, **kw)
    finally:
        if log_path:
            out.close()


def run_hidden(cmd, timeout):
    kw = {"creationflags": NO_WINDOW} if IS_WINDOWS else {}
    return subprocess.run(cmd, capture_output=True, timeout=timeout, **kw)


def restrict_permissions(path):
    """Owner-only access. Returns (ok, message)."""
    path = Path(path)
    if IS_WINDOWS:
        user = os.environ.get("USERNAME") or getpass.getuser()
        r = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r",
                            f"{user}:(OI)(CI)F"], capture_output=True, text=True,
                           creationflags=NO_WINDOW)
        if r.returncode == 0:
            return True, "ok"
        return False, (r.stderr or r.stdout or "icacls failed").strip()
    try:
        if path.is_dir():
            path.chmod(0o700)
            for p in path.rglob("*"):
                if p.is_symlink():
                    continue
                p.chmod(0o700 if p.is_dir() else 0o600)
        else:
            path.chmod(0o600)
    except OSError as e:
        return False, str(e)
    return True, "ok"


_BROAD = ("everyone", "builtin\\users", "nt authority\\authenticated users")


def permissions_ok(path):
    """True if no group/other access (POSIX). Windows: False if icacls shows broad principals."""
    if IS_WINDOWS:
        try:
            r = run_hidden(["icacls", str(path)], 30)
            out = r.stdout.decode("utf-8", "replace") if isinstance(r.stdout, bytes) else (r.stdout or "")
        except Exception:
            return True
        if r.returncode != 0:
            return True
        low = out.lower()
        return not any(b in low for b in _BROAD)
    try:
        return (Path(path).stat().st_mode & 0o077) == 0
    except OSError:
        return False
