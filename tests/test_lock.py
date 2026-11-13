import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

SRC = str(Path(__file__).resolve().parent.parent / "src")


def _child(code, *args):
    env = dict(os.environ, PYTHONPATH=SRC)
    return subprocess.Popen([sys.executable, "-c", code, *args], env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)


HOLDER = """
import os, sys
from pathlib import Path
from telegram_bridge import lock
h = lock.acquire(Path(sys.argv[1]))
print('ready', os.getpid(), flush=True)
sys.stdin.readline()
h.release()
"""


@pytest.fixture
def holder(tmp_path):
    path = tmp_path / "bridge.lock"
    p = _child(HOLDER, str(path))
    word, pid = p.stdout.readline().split()
    assert word == "ready"
    p.real_pid = int(pid)  # the venv launcher makes Popen.pid differ from the interpreter's pid
    yield path, p
    if p.poll() is None:
        p.stdin.write("\n")
        p.stdin.flush()
        p.wait(timeout=5)


def test_acquire_writes_info_and_releases(tmp_path):
    from telegram_bridge import lock
    path = tmp_path / "bridge.lock"
    h = lock.acquire(path)
    info = lock.read_info(path)
    assert info["pid"] == os.getpid() and "started" in info
    assert lock.is_held(path) is True
    h.release()
    assert lock.is_held(path) is False
    lock.acquire(path).release()


def test_second_process_refused_and_info_readable(holder):
    from telegram_bridge import lock
    path, p = holder
    with pytest.raises(lock.AlreadyRunning) as e:
        lock.acquire(path)
    assert e.value.pid == p.real_pid
    assert lock.read_info(path)["pid"] == p.real_pid
    assert lock.is_held(path) is True


def test_released_after_holder_exits(holder):
    from telegram_bridge import lock
    path, p = holder
    p.stdin.write("\n")
    p.stdin.flush()
    p.wait(timeout=5)
    assert lock.is_held(path) is False
    lock.acquire(path).release()


def test_stale_garbage_file_does_not_block(tmp_path):
    from telegram_bridge import lock
    path = tmp_path / "bridge.lock"
    path.write_bytes(b"\x00garbage{{{ not json")
    assert lock.read_info(path) is None
    assert lock.is_held(path) is False
    h = lock.acquire(path)
    assert lock.read_info(path)["pid"] == os.getpid()
    h.release()


def test_is_held_missing_file(tmp_path):
    from telegram_bridge import lock
    assert lock.is_held(tmp_path / "nope.lock") is False


def test_cli_run_refuses_when_held(holder, monkeypatch, capsys):
    from telegram_bridge import bridge, cli, settings
    path, p = holder
    monkeypatch.setattr(settings, "LOCK", path)
    called = []

    async def stub():
        called.append(1)

    monkeypatch.setattr(bridge, "main", stub)
    assert cli.main(["run"]) == 3
    assert f"already running (pid {p.real_pid})" in capsys.readouterr().out
    assert cli.main(["run", "--service"]) == 0
    assert called == []


def test_cli_run_releases_lock(tmp_path, monkeypatch):
    from telegram_bridge import bridge, cli, lock, settings
    path = tmp_path / "bridge.lock"
    monkeypatch.setattr(settings, "LOCK", path)
    seen = []

    async def stub():
        seen.append(lock.is_held(path))

    monkeypatch.setattr(bridge, "main", stub)
    assert cli.main(["run"]) == 0
    assert seen == [True]
    assert lock.is_held(path) is False


def test_conflict_logged_once(caplog):
    from telegram.error import Conflict, NetworkError
    from telegram_bridge import bridge
    svc = bridge.BotService.__new__(bridge.BotService)
    svc.name = "main"
    with caplog.at_level(logging.INFO, logger="bridge"):
        for _ in range(3):
            svc.on_polling_error(Conflict("terminated by other getUpdates"))
        for _ in range(3):
            svc.on_polling_error(NetworkError("boom"))
    msgs = [r.getMessage() for r in caplog.records]
    assert sum("409 Conflict" in m for m in msgs) == 1
    assert ("[main] another program is polling this bot token (409 Conflict). "
            "Stop the other copy or use a different token.") in msgs
    assert sum("NetworkError" in m for m in msgs) == 1
