import json
import sys
from pathlib import Path

import pyotp
import pytest

from telegram_bridge.auth import Auth
from telegram_bridge.runner import build_cmd, parse_event
from telegram_bridge.textutil import split_message

SECRET = pyotp.random_base32()


class Clock:
    def __init__(self, t=1_700_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def mk(ids=(42,), **kw):
    c = Clock()
    return Auth(ids, SECRET, clock=c, **kw), c


def code(c, offset=0):
    return pyotp.TOTP(SECRET).at(c.t + offset)


def test_whitelist():
    a, _ = mk()
    assert a.is_allowed(42) and not a.is_allowed(7)


def test_whoami_only_when_empty():
    assert mk(ids=())[0].whoami_enabled()
    assert not mk(ids=(42,))[0].whoami_enabled()


def test_unlock_valid_and_expiry():
    a, c = mk(session_hours=12)
    ok, _ = a.unlock(42, code(c))
    assert ok and a.is_unlocked(42)
    c.t += 12 * 3600 - 1
    assert a.is_unlocked(42)
    c.t += 2
    assert not a.is_unlocked(42)


def test_unlock_invalid_and_non_whitelisted():
    a, c = mk()
    assert not a.unlock(42, "000000")[0] or code(c) == "000000"
    assert not a.unlock(42, "abc")[0]
    assert not a.unlock(7, code(c))[0]
    assert not a.is_unlocked(7)


def test_replay_rejected():
    a, c = mk()
    cd = code(c)
    assert a.unlock(42, cd)[0]
    a.lock(42)
    assert not a.is_unlocked(42)
    assert not a.unlock(42, cd)[0]


def test_lockout_after_5_failures_then_recovers():
    a, c = mk(max_failures=5, lockout_minutes=15)
    bad = "123456" if code(c) != "123456" else "654321"
    for _ in range(5):
        assert not a.unlock(42, bad)[0]
    ok, msg = a.unlock(42, code(c))
    assert not ok and "Locked out" in msg
    c.t += 15 * 60 + 1
    assert a.unlock(42, code(c))[0]


def test_lock():
    a, c = mk()
    a.unlock(42, code(c))
    a.lock(42)
    assert not a.is_unlocked(42)


def test_split_short_and_long():
    assert split_message("hi") == ["hi"]
    text = ("word " * 50 + "\n") * 200
    parts = split_message(text, 4096)
    assert all(len(p) <= 4096 for p in parts) and len(parts) > 1
    assert "".join(parts).replace("\n", "").replace(" ", "") == text.replace("\n", "").replace(" ", "")


def test_split_no_whitespace():
    parts = split_message("x" * 10000, 4096)
    assert [len(p) for p in parts] == [4096, 4096, 1808]


STREAM = "\n".join(json.dumps(e) for e in [
    {"type": "system", "subtype": "init", "session_id": "abc-123"},
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "OK"}]}},
    {"type": "result", "subtype": "success", "is_error": False, "result": "OK", "session_id": "abc-123"},
]) + "\nnot json\n"


def test_parse_stream_json():
    evs = [e for e in (parse_event(l) for l in STREAM.splitlines()) if e]
    assert [e["kind"] for e in evs] == ["session", "tool", "text", "result"]
    assert evs[0]["session_id"] == "abc-123"
    assert evs[1]["name"] == "Bash" and evs[1]["detail"] == "ls"
    assert evs[3]["text"] == "OK" and not evs[3]["is_error"]


def test_build_cmd_resume():
    cfg = {"command": "claude", "args": ["-p", "{prompt}"], "resume_args": ["--resume", "{session_id}"]}
    assert build_cmd(cfg, "hi") == ["claude", "-p", "hi"]
    assert build_cmd(cfg, "hi", "sid") == ["claude", "-p", "hi", "--resume", "sid"]


def test_persona_per_bot():
    base = {"command": "claude", "args": ["-p"]}
    claude = " ".join(build_cmd({**base, "append_system_prompt": "Your name is A. Be masculine. subagent"}, "hi"))
    qwen = " ".join(build_cmd({**base, "append_system_prompt": "Your name is B. Be feminine. AGENTS.md brain"}, "hi"))
    assert "Your name is A" in claude and "subagent" in claude.lower()
    assert "Your name is B" not in claude and "feminine" not in claude
    assert "Your name is B" in qwen and "AGENTS.md" in qwen and "brain" in qwen
    assert "Your name is A" not in qwen and "masculine" not in qwen
