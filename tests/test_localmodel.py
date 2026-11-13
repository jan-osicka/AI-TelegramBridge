import asyncio
import sys
from pathlib import Path

import pytest

from telegram_bridge import settings
from telegram_bridge.auth import Auth
from telegram_bridge.bridge import BotService, commands_for
from telegram_bridge.localmodel import LocalModelError, LocalModelManager
from telegram_bridge.runner import build_cmd


class FakeServer:
    """Mocked HTTP check + process launcher. Server 'comes up' after `delay` polls."""
    def __init__(self, delay=2):
        self.up, self.launched, self.stopped, self.delay, self.polls = False, 0, 0, delay, 0

    def check(self, url):
        if self.launched and not self.up:
            self.polls += 1
            if self.polls >= self.delay:
                self.up = True
        return self.up

    def launch(self, cmd, log_path=None):
        self.launched += 1

    def stop(self, cmd):
        self.stopped += 1
        self.up = False


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def mk(server, clock=None, **kw):
    return LocalModelManager("http://x", ["start"], ["stop"], poll_s=0.01, check=server.check,
                       launcher=server.launch, stopper=server.stop, clock=clock or Clock(), **kw)


def test_single_launch_under_concurrency():
    srv = FakeServer(delay=5)
    m = mk(srv, clock=__import__("time").monotonic)

    async def go():
        return await asyncio.gather(*[m.ensure() for _ in range(6)])

    res = asyncio.run(go())
    assert srv.launched == 1
    assert res.count(True) == 1 and srv.up


def test_already_up_no_launch():
    srv = FakeServer()
    srv.up = True
    assert asyncio.run(mk(srv).ensure()) is False
    assert srv.launched == 0


def test_timeout():
    srv = FakeServer(delay=10**9)
    clock = Clock()
    m = mk(srv, clock=clock, startup_timeout_s=360)
    real_sleep = asyncio.sleep

    async def fake_sleep(s):
        clock.t += 100  # advance fake time
        await real_sleep(0)

    async def go():
        orig = asyncio.sleep
        asyncio.sleep = fake_sleep
        try:
            await m.ensure()
        finally:
            asyncio.sleep = orig

    with pytest.raises(LocalModelError, match="did not become ready"):
        asyncio.run(go())
    assert srv.launched == 1


def test_launch_failure():
    srv = FakeServer()
    srv.launch = lambda cmd, log_path=None: (_ for _ in ()).throw(OSError("boom"))
    m = mk(srv)
    m.launcher = srv.launch
    with pytest.raises(LocalModelError, match="could not launch"):
        asyncio.run(m.ensure())


def test_idle_stop():
    srv = FakeServer()
    srv.up = True
    clock = Clock()
    m = mk(srv, clock=clock, idle_stop_minutes=20)

    async def go():
        assert not await m.maybe_idle_stop()      # just started
        clock.t = 19 * 60
        assert not await m.maybe_idle_stop()      # not idle long enough
        m.busy = 1
        clock.t = 60 * 60
        assert not await m.maybe_idle_stop()      # busy
        m.busy = 0
        m.touch()
        clock.t += 21 * 60
        assert await m.maybe_idle_stop()          # idle -> stop
        assert not await m.maybe_idle_stop()      # already down

    asyncio.run(go())
    assert srv.stopped == 1


def test_idle_zero_never_stops():
    srv = FakeServer()
    srv.up = True
    clock = Clock()
    m = mk(srv, clock=clock, idle_stop_minutes=0)
    clock.t = 10**7
    assert not asyncio.run(m.maybe_idle_stop())
    assert srv.stopped == 0


# ---- commands / config
class FakeMsg:
    def __init__(self):
        self.texts = []

    async def reply_text(self, t, **kw):
        self.texts.append(t)


class FakeUpdate:
    def __init__(self):
        self.effective_message = FakeMsg()


def test_exit_and_new_clear_session():
    cfg = settings.load_config()
    for alias in ("exit", "new"):
        svc = BotService("claude", cfg["bots"]["claude"], "t", Auth([1], "JBSWY3DPEB3W64TMMQ"), cfg["general"])
        svc.sessions[5] = "sid"
        up = FakeUpdate()
        asyncio.run(svc.command(up, alias, "", 1, 5))
        assert 5 not in svc.sessions
        assert up.effective_message.texts == ["Context cleared."]


def test_lock_works_on_both_bots_shared_auth():
    cfg = settings.load_config()
    auth = Auth([1], "JBSWY3DPEB3W64TMMQ")
    auth._expiry[1] = auth.clock() + 1000
    a = BotService("claude", cfg["bots"]["claude"], "t", auth, cfg["general"])
    b = BotService("qwen", cfg["bots"]["qwen"], "t", auth, cfg["general"])
    assert a.auth is b.auth and auth.is_unlocked(1)
    asyncio.run(b.command(FakeUpdate(), "lock", "", 1, 5))
    assert not auth.is_unlocked(1)


def test_claude_cmd_has_subagent_prompt_qwen_does_not():
    cfg = settings.load_config()["bots"]
    c = build_cmd(cfg["claude"], "hi", "sid")
    assert "--append-system-prompt" in c
    assert "subagents" in c[c.index("--append-system-prompt") + 1]
    assert "--resume" in c
    for bot in ("claude", "qwen"):
        cmd = build_cmd(cfg[bot], "hi")
        assert "brain" in cmd[cmd.index("--append-system-prompt") + 1]
    q = build_cmd(cfg["qwen"], "hi")
    assert "subagents" not in q[q.index("--append-system-prompt") + 1]
    q2 = dict(cfg["qwen"], append_system_prompt="")
    assert "--append-system-prompt" not in build_cmd(q2, "hi")


def test_qwen_extra_commands_only_on_qwen_bot():
    cfg = settings.load_config()["bots"]
    names = lambda b: [c for c, _ in commands_for(cfg[b])]
    assert "modelstop" in names("qwen") and "modelstart" in names("qwen")
    assert "modelstop" not in names("claude")
    assert "exit" in names("claude") and "exit" in names("qwen")
