import asyncio
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from telegram_bridge import osutil

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX only")


def test_spawn_kwargs_per_os(monkeypatch):
    monkeypatch.setattr(osutil, "IS_WINDOWS", True)
    assert osutil.spawn_kwargs() == {"creationflags": osutil.NO_WINDOW}
    monkeypatch.setattr(osutil, "IS_WINDOWS", False)
    assert osutil.spawn_kwargs() == {"start_new_session": True}


def test_kill_tree_windows_uses_taskkill(monkeypatch):
    calls = []
    monkeypatch.setattr(osutil, "IS_WINDOWS", True)
    monkeypatch.setattr(osutil.subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw)))
    osutil.kill_tree(4321)
    assert calls[0][0] == ["taskkill", "/T", "/F", "/PID", "4321"]
    assert calls[0][1]["creationflags"] == osutil.NO_WINDOW


def test_kill_tree_posix_sends_term_only_and_returns(monkeypatch):
    sent = []
    monkeypatch.setattr(osutil, "IS_WINDOWS", False)
    monkeypatch.setattr(osutil.os, "killpg", lambda pid, sig: sent.append((pid, sig)), raising=False)
    osutil.kill_tree(77)
    assert sent == [(77, signal.SIGTERM)]


@pytest.mark.parametrize("exc", [ProcessLookupError, PermissionError])
def test_kill_tree_gone_process_is_quiet(monkeypatch, exc):
    def gone(pid, sig):
        raise exc
    monkeypatch.setattr(osutil, "IS_WINDOWS", False)
    monkeypatch.setattr(osutil.os, "killpg", gone, raising=False)
    osutil.kill_tree(77)
    osutil.force_kill_tree(77)


class FakeProc:
    pid = 55

    def __init__(self, exits_after):
        self.returncode = None
        self._ev = asyncio.Event()
        self.exits_after = exits_after

    async def wait(self):
        if self.exits_after is not None:
            await asyncio.sleep(self.exits_after)
            self.returncode = 0
            return 0
        await self._ev.wait()
        return 0


def _killpg_recorder(monkeypatch, proc=None):
    sent = []

    def fake(pid, sig):
        sent.append(sig)
        if sig == getattr(signal, "SIGKILL", 9) and proc is not None:
            proc.returncode = 0
            proc._ev.set()
    monkeypatch.setattr(osutil, "IS_WINDOWS", False)
    monkeypatch.setattr(osutil.os, "killpg", fake, raising=False)
    return sent


def test_terminate_tree_no_sigkill_if_child_exits(monkeypatch):
    proc = FakeProc(exits_after=0.01)
    sent = _killpg_recorder(monkeypatch)
    asyncio.run(osutil.terminate_tree(proc, grace=1))
    assert sent == [signal.SIGTERM]


def test_terminate_tree_escalates_and_does_not_block_loop(monkeypatch):
    proc = FakeProc(exits_after=None)
    sent = _killpg_recorder(monkeypatch, proc)
    ticks = []

    async def ticker():
        while True:
            ticks.append(1)
            await asyncio.sleep(0.01)

    async def main():
        t = asyncio.create_task(ticker())
        await osutil.terminate_tree(proc, grace=0.2)
        t.cancel()

    asyncio.run(main())
    assert sent == [signal.SIGTERM, getattr(signal, "SIGKILL", 9)]
    assert len(ticks) >= 5  # the loop kept running during the grace wait


def test_run_hidden_passes_flags(monkeypatch):
    seen = {}

    def fake(cmd, **kw):
        seen.update(kw, cmd=cmd)
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(osutil, "IS_WINDOWS", True)
    monkeypatch.setattr(osutil.subprocess, "run", fake)
    r = osutil.run_hidden(["x"], 9)
    assert r.returncode == 0 and seen["timeout"] == 9
    assert seen["creationflags"] == osutil.NO_WINDOW and seen["capture_output"]


def test_launch_detached_flags(monkeypatch):
    seen = {}
    monkeypatch.setattr(osutil.subprocess, "Popen", lambda cmd, **kw: seen.update(kw))
    monkeypatch.setattr(osutil, "IS_WINDOWS", True)
    osutil.launch_detached(["x"], None)
    assert seen["creationflags"] == osutil.NEW_GROUP | osutil.NO_WINDOW
    monkeypatch.setattr(osutil, "IS_WINDOWS", False)
    seen.clear()
    osutil.launch_detached(["x"], None)
    assert seen["start_new_session"] is True and "creationflags" not in seen


def test_restrict_permissions_windows_icacls(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(osutil, "IS_WINDOWS", True)
    monkeypatch.setenv("USERNAME", "someone")
    monkeypatch.setattr(osutil.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    ok, _ = osutil.restrict_permissions(tmp_path)
    assert ok
    assert calls[0] == ["icacls", str(tmp_path), "/inheritance:r", "/grant:r", "someone:(OI)(CI)F"]


@posix_only
def test_restrict_permissions_posix_modes(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    f = d / "f.txt"
    f.write_text("x")
    f.chmod(0o644)
    ok, _ = osutil.restrict_permissions(d)
    assert ok
    assert (d.stat().st_mode & 0o777) == 0o700
    assert (f.stat().st_mode & 0o777) == 0o600
    assert osutil.permissions_ok(d)


@posix_only
def test_permissions_ok_detects_world_readable(tmp_path):
    f = tmp_path / "f"
    f.write_text("x")
    f.chmod(0o644)
    assert not osutil.permissions_ok(f)
    f.chmod(0o600)
    assert osutil.permissions_ok(f)


ICACLS_BAD = "C:\\x NT AUTHORITY\\SYSTEM:(F)\n   Everyone:(R)\n"
ICACLS_GOOD = "C:\\x BUILTIN\\Administrators:(F)\n   PC\\me:(OI)(CI)(F)\n"


def _icacls(monkeypatch, out, rc=0):
    monkeypatch.setattr(osutil, "IS_WINDOWS", True)
    monkeypatch.setattr(osutil.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, rc, out.encode(), b""))


@pytest.mark.parametrize("who", ["Everyone:(R)", "BUILTIN\\Users:(RX)", "NT AUTHORITY\\Authenticated Users:(M)"])
def test_permissions_ok_windows_detects_broad(monkeypatch, who):
    _icacls(monkeypatch, "C:\\x " + who + "\n")
    assert osutil.permissions_ok("C:\\x") is False


def test_permissions_ok_windows_clean_and_failure(monkeypatch):
    _icacls(monkeypatch, ICACLS_GOOD)
    assert osutil.permissions_ok("C:\\x") is True
    _icacls(monkeypatch, ICACLS_BAD, rc=5)
    assert osutil.permissions_ok("C:\\x") is True

    def boom(cmd, **kw):
        raise OSError("no icacls")
    monkeypatch.setattr(osutil.subprocess, "run", boom)
    assert osutil.permissions_ok("C:\\x") is True


@posix_only
def test_restrict_permissions_skips_symlinks(tmp_path):
    target = tmp_path / "t"
    target.write_text("x")
    target.chmod(0o644)
    d = tmp_path / "d"
    d.mkdir()
    (d / "l").symlink_to(target)
    osutil.restrict_permissions(d)
    assert (target.stat().st_mode & 0o777) == 0o644
