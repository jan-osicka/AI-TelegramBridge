import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pyotp

from telegram_bridge import settings
from telegram_bridge.auth import Auth
from telegram_bridge.bridge import LOCKED_NOTICE, BotService
from telegram_bridge.localmodel import LocalModelManager

SECRET = pyotp.random_base32()


class Clock:
    def __init__(self, t=1_700_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def mk(path, clock, **kw):
    return Auth([42], SECRET, clock=clock, state_path=path, **kw)


def code(c):
    return pyotp.TOTP(SECRET).at(c.t)


def test_unlock_survives_restart(tmp_path):
    p, c = tmp_path / "state.json", Clock()
    a = mk(p, c)
    assert a.unlock(42, code(c))[0]
    b = mk(p, c)
    assert b.is_unlocked(42) and b.expiry(42) == a.expiry(42)
    raw = p.read_text()
    assert SECRET not in raw and code(c) not in raw
    assert not list(tmp_path.glob("*.tmp"))


def test_expired_state_is_locked(tmp_path):
    p, c = tmp_path / "state.json", Clock()
    mk(p, c, session_hours=1).unlock(42, code(c))
    c.t += 3601
    assert not mk(p, c, session_hours=1).is_unlocked(42)


def test_lock_clears_file(tmp_path):
    p, c = tmp_path / "state.json", Clock()
    a = mk(p, c)
    a.unlock(42, code(c))
    a.lock(42)
    assert not mk(p, c).is_unlocked(42)
    if p.exists():
        assert json.loads(p.read_text())["expiry"] == {}
    c.t += 600  # replay counter has aged out -> file removed entirely
    a._save()
    assert not p.exists()


def test_replay_protection_survives_restart(tmp_path):
    p, c = tmp_path / "state.json", Clock()
    a = mk(p, c)
    used = code(c)
    assert a.unlock(42, used)[0]
    a.lock(42)
    b = mk(p, c)
    assert not b.unlock(42, used)[0]


def test_lockout_survives_restart(tmp_path):
    p, c = tmp_path / "state.json", Clock()
    a = mk(p, c, max_failures=2)
    a.unlock(42, "000000")
    a.unlock(42, "000001")
    b = mk(p, c, max_failures=2)
    assert b.lockout_remaining(42) > 0
    assert "Locked out" in b.unlock(42, code(c))[1]


def test_corrupt_state_stays_locked(tmp_path):
    p, c = tmp_path / "state.json", Clock()
    p.write_text("{not json")
    assert not mk(p, c).is_unlocked(42)


class FakeMsg:
    def __init__(self, text, chat_id=5):
        self.text, self.caption, self.chat_id = text, None, chat_id
        self.texts = []

    async def reply_text(self, t, **kw):
        self.texts.append(t)

    async def delete(self):
        pass


def upd(uid, text, chat_id=5):
    m = FakeMsg(text, chat_id)
    return SimpleNamespace(effective_message=m, effective_user=SimpleNamespace(id=uid)), m


def svc(name, auth, clock):
    cfg = settings.load_config()
    return BotService(name, cfg["bots"][name], "t", auth, cfg["general"], clock=clock)


def test_locked_notice_rate_limited_and_only_for_whitelisted():
    auth = Auth([1], SECRET)
    clk = Clock(0.0)
    s = svc("qwen", auth, clk)
    u, m = upd(1, "hello")
    asyncio.run(s.on_update(u, None))
    assert m.texts == [LOCKED_NOTICE]
    clk.t = 30
    u, m = upd(1, "again")
    asyncio.run(s.on_update(u, None))
    assert m.texts == []
    clk.t = 61
    u, m = upd(1, "later")
    asyncio.run(s.on_update(u, None))
    assert m.texts == [LOCKED_NOTICE]
    u, m = upd(1, "other chat", chat_id=9)
    asyncio.run(s.on_update(u, None))
    assert m.texts == [LOCKED_NOTICE]
    u, m = upd(999, "stranger")
    asyncio.run(s.on_update(u, None))
    assert m.texts == []


def test_unlock_works_on_both_bots(tmp_path):
    for name in ("claude", "qwen"):
        c = Clock()
        auth = mk(tmp_path / f"{name}.json", c)
        s = svc(name, auth, Clock(0.0))
        sent = []

        async def send_message(chat, text):
            sent.append(text)
        u, _ = upd(42, f"/unlock {code(c)}")
        asyncio.run(s.on_update(u, SimpleNamespace(bot=SimpleNamespace(send_message=send_message))))
        assert auth.is_unlocked(42) and sent and sent[0].startswith("\U0001F513")


def test_adopt_running_strata_starts_idle_timer_and_stops():
    clk = Clock(0.0)
    state = {"up": True, "stopped": 0}

    def stop(cmd):
        state["stopped"] += 1
        state["up"] = False

    q = LocalModelManager("http://x", ["s"], ["stop"], idle_stop_minutes=20, check=lambda u: state["up"],
                    stopper=stop, clock=clk)
    clk.t = 5000  # long after construction
    assert asyncio.run(q.adopt_running()) is True
    assert q.last_activity == 5000
    clk.t += 19 * 60
    assert asyncio.run(q.maybe_idle_stop()) is False
    clk.t += 2 * 60
    assert asyncio.run(q.maybe_idle_stop()) is True and state["stopped"] == 1


def test_adopt_when_not_running():
    q = LocalModelManager("http://x", ["s"], ["stop"], check=lambda u: False)
    assert asyncio.run(q.adopt_running()) is False


def test_idle_stop_skipped_while_external_client_busy(tmp_path):
    clk = Clock(0.0)
    state = {"up": True, "stopped": 0}
    busy = tmp_path / "busy"
    busy.write_text("1")
    wall = {"t": busy.stat().st_mtime}

    def stop(cmd):
        state["stopped"] += 1
        state["up"] = False

    q = LocalModelManager("http://x", ["s"], ["stop"], idle_stop_minutes=20, check=lambda u: state["up"],
                    stopper=stop, clock=clk, busy_file=str(busy), wall=lambda: wall["t"])
    clk.t = 21 * 60
    wall["t"] += 60                      # heartbeat 1 min old -> still busy
    assert asyncio.run(q.maybe_idle_stop()) is False and state["stopped"] == 0
    clk.t += 21 * 60
    wall["t"] += 10 * 60                 # heartbeat stale -> stop
    assert asyncio.run(q.maybe_idle_stop()) is True and state["stopped"] == 1
