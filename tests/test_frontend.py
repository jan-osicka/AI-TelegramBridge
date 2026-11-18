"""Pass 2: footer/warnings, HTML sending, threading, heartbeat, typing, startup notice, Qwen notices, idle touch."""
import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pyotp
import pytest
from telegram.error import BadRequest

from telegram_bridge import bridge
from telegram_bridge import chat as chatmod
from telegram_bridge import ctxinfo
from telegram_bridge import settings
from telegram_bridge.auth import Auth
from telegram_bridge.bridge import BotService
from telegram_bridge.runner import RunStats
from telegram_bridge.state import StateStore

SECRET = pyotp.random_base32()


class Clock:
    def __init__(self, t=1_700_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class Status:
    def __init__(self, bot):
        self.bot, self.deleted = bot, False

    async def edit_text(self, text):
        if self.bot.edit_error:
            raise self.bot.edit_error
        self.bot.edits.append(text)

    async def delete(self):
        self.deleted = True


class FakeBot:
    def __init__(self):
        self.sent, self.docs, self.edits, self.actions = [], [], [], []
        self.edit_error = None

    async def send_message(self, chat, text, **kw):
        if kw.get("parse_mode") == "HTML" and "BAD" in text:
            raise BadRequest("Can't parse entities")
        self.sent.append({"chat": chat, "text": text, **kw})
        return Status(self)

    async def send_document(self, chat, f, filename=None, **kw):
        self.docs.append((chat, f.read(), filename))

    async def send_chat_action(self, chat, action):
        self.actions.append(action)

    def texts(self):
        return [m["text"] for m in self.sent]


def make(name="claude", store=None, auth=None, **general):
    cfg = settings.load_config()
    s = BotService(name, cfg["bots"][name], "t", auth or Auth([5], SECRET), {**cfg["general"], **general},
                   clock=Clock(0.0), store=store)
    s.app = SimpleNamespace(bot=FakeBot())
    return s


def script_run(s, final="done", sid="sid-1", is_err=False, stats=None, on_events=(), delay=0.0):
    async def run(prompt, session_id=None, on_event=None):
        s.runner.stats = stats or RunStats()
        for ev in on_events:
            await on_event(ev)
        if delay:
            await asyncio.sleep(delay)
        return final, sid, is_err
    s.runner.run = run


def stats(tokens=84000, window=200000, cost=0.18, compacted=False):
    return RunStats(tokens, window, cost, compacted, "m")


def run_task(s, prompt="hi", reply_to=77):
    asyncio.run(s.run_task(5, prompt, reply_to))
    return s.app.bot


# ---- footer and warnings
def test_claude_footer_no_cost_and_threading():
    s = make()
    script_run(s, "Hello **world**", stats=stats())
    bot = run_task(s)
    status, answer = bot.sent[0], bot.sent[1]
    assert status["reply_parameters"].message_id == 77 and status["reply_parameters"].allow_sending_without_reply
    assert answer["text"] == "Hello <b>world</b>\n\n· 42% context" and answer["parse_mode"] == "HTML"
    assert answer["reply_parameters"].message_id == 77
    assert s.ctx[5]["pct"] == 42 and s.sessions[5] == "sid-1"


def test_qwen_footer_never_shows_cost():
    s = make("qwen")
    script_run(s, "ok", stats=stats(tokens=26214, window=131072, cost=0.5))
    bot = run_task(s)
    assert bot.sent[1]["text"] == "ok\n\n· 20% context"


def test_no_footer_without_usage_or_when_error_stopped():
    s = make()
    script_run(s, "ok", stats=RunStats())
    assert run_task(s).sent[1]["text"] == "ok"
    s = make()
    script_run(s, "boom", is_err=True, stats=stats())
    bot = run_task(s)
    assert bot.sent[1]["text"].startswith("⚠️ boom") and 5 not in s.sessions and 5 not in s.ctx


def test_warnings_once_at_70_then_90_and_persist(tmp_path):
    store = StateStore(tmp_path / "s.json")
    s = make(store=store)
    script_run(s, "a", stats=stats(tokens=142000))      # 71%
    bot = run_task(s)
    assert bot.texts()[-1].startswith("⚠️ Context 71% full. Claude will auto-compact soon")
    script_run(s, "b", stats=stats(tokens=150000))
    n = len(bot.sent)
    run_task(s)
    assert len(bot.sent) == n + 2 and not any("Context 75%" in t for t in bot.texts())   # status + answer only
    s2 = make(store=store)                                # restart: flags survive
    assert s2.ctx[5]["warned"] == [70] and s2.ctx[5]["pct"] == 75
    script_run(s2, "c", stats=stats(tokens=182000))      # 91%
    bot2 = run_task(s2)
    assert bot2.texts()[-1].startswith("🚨 Context 91% full")
    script_run(s2, "d", stats=stats(tokens=190000))
    bot3 = run_task(s2)
    assert not any("Context 95%" in t for t in bot3.texts())


def test_flags_reset_on_new_session_id_exit_and_cwd(tmp_path):
    s = make(allowed_cwd_roots=[str(tmp_path)])
    script_run(s, "a", stats=stats(tokens=150000))
    run_task(s)
    assert s.ctx[5]["warned"] == [70]
    script_run(s, "b", sid="sid-2", stats=stats(tokens=150000))   # new session id: flags start over
    bot = run_task(s)
    assert bot.texts()[-1].startswith("⚠️ Context 75%")
    s.reset_session(5)
    assert 5 not in s.ctx and 5 not in s.sessions
    script_run(s, "c", stats=stats(tokens=150000))
    s.ctx[5] = {"tokens": 1, "window": 2, "pct": 50, "warned": [70], "trimmed": True}

    async def go():
        msg = SimpleNamespace(reply_text=lambda t, **kw: asyncio.sleep(0))
        await s.command(SimpleNamespace(effective_message=msg), "cwd", str(tmp_path), 5, 5)
    asyncio.run(go())
    assert 5 not in s.ctx


def test_compaction_notice_and_rearm():
    s = make()
    script_run(s, "a", stats=stats(tokens=182000))
    run_task(s)
    assert s.ctx[5]["warned"] == [70, 90]
    script_run(s, "b", stats=stats(tokens=20000, compacted=True))
    bot = run_task(s)
    assert ctxinfo.COMPACTED_NOTICE in bot.texts()
    assert s.ctx[5]["warned"] == [] and s.ctx[5]["pct"] == 10


def test_status_command_shows_context():
    s = make()
    s.sessions[5] = "x"
    s.ctx[5] = {"tokens": 84000, "window": 200000, "pct": 42, "warned": [], "trimmed": False}
    assert s.context_status(5) == ["Context: 42% (84k of 200k tokens)"]
    assert s.context_status(6) == []
    del s.sessions[5]
    assert s.context_status(5) == []


# ---- HTML sending
def test_html_rejected_falls_back_to_plain_per_chunk():
    s = make()
    asyncio.run(s.send_rich(5, "BAD **bold** text", reply_to=9))
    m = s.app.bot.sent[0]
    assert m["text"] == "BAD **bold** text" and "parse_mode" not in m and m["reply_parameters"].message_id == 9


def test_long_reply_chunks_only_first_is_threaded_and_code_survives():
    s = make()
    text = "```py\n" + "\n".join(f"v{i} = 1 < 2" for i in range(1200)) + "\n```"
    asyncio.run(s.send_rich(5, text, reply_to=3, footer="· 5% context"))
    sent = s.app.bot.sent
    assert len(sent) >= 3 and "reply_parameters" in sent[0] and all("reply_parameters" not in m for m in sent[1:])
    assert all(m["parse_mode"] == "HTML" and len(m["text"]) <= 4096 for m in sent)
    assert sent[-1]["text"].endswith("· 5% context")


def test_long_answer_preview_and_document_then_footer():
    s = make(document_threshold_chars=1000)
    answer = "# T\n" + "x < y **b** " * 400
    script_run(s, answer, stats=stats())
    bot = run_task(s)
    assert bot.sent[1]["text"].startswith("<b>T</b>") and bot.sent[1]["text"].endswith("(full answer attached)")
    assert bot.docs[0][1].decode().startswith("# T") and bot.docs[0][2] == "answer.md"
    assert bot.sent[2]["text"] == "· 42% context"


def test_reply_kw_none_when_no_message():
    assert bridge.reply_kw(None) == {} and bridge.reply_kw(0) == {}


def test_queued_notice_and_item_carry_message_id():
    s = make(auth=Auth([5], SECRET, clock=Clock()))
    a = s.auth
    assert a.unlock(5, pyotp.TOTP(SECRET).at(a.clock()))[0]
    calls = []

    class M:
        text, caption, chat_id, message_id = "hello", None, 5, 321
        voice = audio = photo = document = None

        async def reply_text(self, t, **kw):
            calls.append((t, kw))

    async def go():
        s.runner.proc = SimpleNamespace(returncode=None)      # busy
        await s.on_update(SimpleNamespace(effective_message=M(), effective_user=SimpleNamespace(id=5)), None)
    asyncio.run(go())
    assert calls[0][1] == {"do_quote": True, "allow_sending_without_reply": True} and "Queued" in calls[0][0]
    assert s.queue.get_nowait()[2]["msg_id"] == 321


def test_work_threads_errors_and_dropped_notice():
    s = make()

    async def go():
        await s.queue.put((5, 5, {"mode": "agent", "prompt": "x", "text": "", "images": [], "msg_id": 11}))
        t = asyncio.create_task(s.work())
        await asyncio.sleep(0.1)
        t.cancel()
    asyncio.run(go())      # not unlocked: dropped, threaded
    m = s.app.bot.sent[0]
    assert "queued task dropped" in m["text"] and m["reply_parameters"].message_id == 11


# ---- heartbeat
def test_heartbeat_edits_without_tool_events_and_stops(monkeypatch):
    monkeypatch.setattr(bridge, "fmt_elapsed", lambda sec: f"{sec:.2f}s")   # sub-second ticks would look identical
    s = make(edit_interval_seconds=0.05, heartbeat_seconds=0.1)
    script_run(s, "done", stats=stats(), delay=0.55)
    bot = run_task(s)
    assert len(bot.edits) >= 3
    assert all(e.startswith("⏳ Claude is working… ") for e in bot.edits) and "step" not in bot.edits[0]
    n = len(bot.edits)
    asyncio.run(asyncio.sleep(0))
    assert len(bot.edits) == n                      # the task was cancelled with the run


def test_heartbeat_shows_steps_and_last_tool_and_respects_min_spacing():
    s = make(edit_interval_seconds=0.3, heartbeat_seconds=5)
    evs = [{"kind": "tool", "name": "Bash", "detail": "ls"}] * 5 + [{"kind": "text", "text": "x"}]
    script_run(s, "done", on_events=evs, stats=stats())
    bot = run_task(s)
    assert bot.edits == []        # 5 tool events inside one interval: no edit storm (first edit not before 0.3 s)

    s = make(edit_interval_seconds=0.0, heartbeat_seconds=5)
    script_run(s, "done", on_events=evs[:2], stats=stats())
    bot = run_task(s)
    assert bot.edits[-1].endswith("step 2\n🔧 Bash: ls") or "step 2" in bot.edits[-1]
    assert "🔧 Bash: ls" in bot.edits[-1]


def test_status_edit_errors_are_ignored():
    s = make(edit_interval_seconds=0.0, heartbeat_seconds=0.05)
    s.app.bot.edit_error = BadRequest("Message is not modified: specified new message content ...")
    script_run(s, "done", stats=stats(), delay=0.2, on_events=[{"kind": "tool", "name": "Bash", "detail": "x"}])
    bot = run_task(s)
    assert bot.sent[-1]["text"].startswith("done")
    s = make(edit_interval_seconds=0.0, heartbeat_seconds=0.05)
    s.app.bot.edit_error = RuntimeError("network")
    script_run(s, "done", stats=stats(), delay=0.2)
    assert run_task(s).sent[-1]["text"].startswith("done")


def test_fmt_elapsed_and_status_text():
    assert bridge.fmt_elapsed(5) == "5s" and bridge.fmt_elapsed(80) == "1m 20s" and bridge.fmt_elapsed(3605) == "60m 05s"
    s = make()
    assert s.status_text(80, 3, "🔧 Bash: ls") == "⏳ Claude is working… 1m 20s · step 3\n🔧 Bash: ls"
    assert s.status_text(4, 0, "") == "⏳ Claude is working… 4s"


# ---- chat mode
def test_chat_typing_repeats_while_waiting_and_html_reply(monkeypatch):
    monkeypatch.setattr(bridge, "TYPING_INTERVAL_S", 0.05)
    s = make("qwen")

    async def slow(url, messages, **kw):
        await asyncio.sleep(0.3)
        return "answer with `code` and **bold**"
    monkeypatch.setattr(chatmod, "complete", slow)
    asyncio.run(s.run_chat(5, {"mode": "chat", "text": "hi", "prompt": "hi", "images": [], "msg_id": 8}))
    bot = s.app.bot
    assert len(bot.actions) >= 4 and set(bot.actions) == {"typing"}
    m = bot.sent[0]
    assert m["text"] == "answer with <code>code</code> and <b>bold</b>" and m["reply_parameters"].message_id == 8
    n = len(bot.actions)
    asyncio.run(asyncio.sleep(0))
    assert len(bot.actions) == n


def test_chat_trim_notice_once_per_chat(monkeypatch):
    s = make("qwen")
    s.chats.max_messages = 4

    async def fake(url, messages, **kw):
        return "r"
    monkeypatch.setattr(chatmod, "complete", fake)

    async def go():
        for i in range(6):
            await s.run_chat(5, {"mode": "chat", "text": f"q{i}", "prompt": "", "images": []})
    asyncio.run(go())
    notes = [t for t in s.app.bot.texts() if t.startswith("ℹ️ Older chat messages")]
    assert notes == ["ℹ️ Older chat messages dropped from my memory (keeps the last 4)."]
    assert s.ctx[5]["trimmed"] is True
    s.reset_session(5)
    assert 5 not in s.ctx


def test_add_reply_reports_dropped():
    cs = chatmod.ChatSessions(max_messages=2)
    cs.messages(1, "s", "a")
    assert cs.add_reply(1, "b") == 0
    cs.messages(1, "s", "c")
    assert cs.add_reply(1, "d") == 2


# ---- startup notice
def test_startup_notice_only_to_whitelisted_chats_with_state(tmp_path):
    store = StateStore(tmp_path / "s.json")
    store.save("claude", {5: "sid", 99: "other"}, {}, {}, {5: {"tokens": 84000, "window": 200000, "pct": 42,
                                                              "warned": [], "trimmed": False}})
    s = make(store=store)
    asyncio.run(s.announce_online())
    assert [(m["chat"], m["text"]) for m in s.app.bot.sent] == [(5, "🟢 Claude is back online. Context kept (42%).")]


def test_startup_notice_without_pct_history_only_and_nothing_persisted(tmp_path):
    store = StateStore(tmp_path / "s.json")
    store.save("qwen", {}, {}, {5: [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]})
    s = make("qwen", store=store)
    asyncio.run(s.announce_online())
    assert s.app.bot.texts() == ["🟢 Qwen is back online. Context kept."]
    assert s.app.bot.sent[0].get("reply_parameters") is None
    empty = make(store=StateStore(tmp_path / "none.json"))
    asyncio.run(empty.announce_online())
    assert empty.app.bot.sent == []
    s2 = make("claude", store=store)
    s2.sessions[5] = "sid"
    asyncio.run(s2.announce_online())
    assert s2.app.bot.texts() == ["🟢 Claude is back online. Context kept."]    # no pct known


def test_startup_notice_send_failure_does_not_raise(tmp_path):
    store = StateStore(tmp_path / "s.json")
    store.save("claude", {5: "sid"}, {}, {})
    s = make(store=store)

    async def boom(*a, **k):
        raise RuntimeError("telegram down")
    s.app.bot.send_message = boom
    asyncio.run(s.announce_online())


# ---- Qwen large session
class FakeQwen:
    def __init__(self, up):
        self.up, self.starting, self.busy = up, False, 0

    async def is_up(self):
        return self.up

    async def ensure(self):
        self.up = True

    def touch(self):
        pass


def qwen_with_session(tokens, up):
    s = make("qwen")
    s.qwen = FakeQwen(up)
    s.sessions[5] = "old"
    s.ctx[5] = {"tokens": tokens, "window": 131072, "pct": 30, "warned": [], "trimmed": False}
    script_run(s, "ok", sid="old", stats=stats(tokens=tokens, window=131072))
    return s


def test_large_session_notice_after_cold_start():
    s = qwen_with_session(46000, up=False)
    asyncio.run(s.run_one(5, {"mode": "agent", "prompt": "hi", "text": "hi", "images": [], "msg_id": 4}))
    t = s.app.bot.texts()
    assert any(x.startswith("⏳ Starting") for x in t)
    note = [x for x in t if x.startswith("⏳ Large session")]
    assert note == ["⏳ Large session (~46k tokens): the first reply after a restart re-reads it, ~20 s. /exit for a fresh start."]
    assert t.index(note[0]) < t.index("⏳ Qwen is working…")


def test_no_large_session_notice_when_warm_small_or_new():
    warm = qwen_with_session(46000, up=True)
    asyncio.run(warm.run_one(5, {"mode": "agent", "prompt": "hi", "text": "hi", "images": []}))
    small = qwen_with_session(29000, up=False)
    asyncio.run(small.run_one(5, {"mode": "agent", "prompt": "hi", "text": "hi", "images": []}))
    new = qwen_with_session(46000, up=False)
    del new.sessions[5]
    asyncio.run(new.run_one(5, {"mode": "agent", "prompt": "hi", "text": "hi", "images": []}))
    for s in (warm, small, new):
        assert not any(x.startswith("⏳ Large session") for x in s.app.bot.texts())


# ---- idle lock refinement
def test_task_completion_counts_as_activity_for_idle_lock():
    c = Clock()
    a = Auth([5], SECRET, clock=c, idle_lock_hours=3)
    assert a.unlock(5, pyotp.TOTP(SECRET).at(c.t))[0]
    s = make(auth=a)
    ran = []

    async def slow_run_one(chat, item):
        ran.append(item["prompt"])
        c.t += 3.5 * 3600            # a long task: longer than the idle limit
    s.run_one = slow_run_one

    async def go():
        for p in ("one", "two"):
            await s.queue.put((5, 5, {"mode": "agent", "prompt": p, "text": "", "images": []}))
        t = asyncio.create_task(s.work())
        for _ in range(50):
            await asyncio.sleep(0.02)
            if len(ran) == 2:
                break
        t.cancel()
    asyncio.run(go())
    assert ran == ["one", "two"]      # the queued task was NOT dropped by the idle lock
    assert not any("dropped" in t for t in s.app.bot.texts())


def test_idle_touch_respects_12h_cap_and_skips_locked():
    c = Clock()
    a = Auth([5], SECRET, clock=c, idle_lock_hours=3, session_hours=12)
    assert a.unlock(5, pyotp.TOTP(SECRET).at(c.t))[0]
    s = make(auth=a)

    async def long_run_one(chat, item):
        c.t += 13 * 3600
    s.run_one = long_run_one

    async def go():
        for p in ("one", "two"):
            await s.queue.put((5, 5, {"mode": "agent", "prompt": p, "text": "", "images": []}))
        t = asyncio.create_task(s.work())
        await asyncio.sleep(0.2)
        t.cancel()
    asyncio.run(go())
    assert not a.is_unlocked(5)                       # the cap still applies
    assert any("queued task dropped" in t for t in s.app.bot.texts())


# ---- redaction on the production path
def test_reply_with_token_is_masked_with_note():
    s = make()
    tok = "123456789:" + "B" * 35
    script_run(s, f"Here is the token {tok} done", stats=stats())
    bot = run_task(s)
    answer = " ".join(bot.texts())
    assert tok not in answer and "BBBBBBBB" not in answer
    assert "[REDACTED]" in answer and "(some secrets were hidden as [REDACTED])" in answer


def test_long_reply_with_token_masks_preview_and_file():
    s = make(document_threshold_chars=1000)
    tok = "ghp_" + "c1" * 18
    script_run(s, "# T\n" + "word " * 400 + tok, stats=stats())
    bot = run_task(s)
    assert tok not in " ".join(bot.texts()) and tok not in bot.docs[0][1].decode()
    assert "[REDACTED]" in bot.docs[0][1].decode()
    assert "(some secrets were hidden as [REDACTED])" in bot.sent[1]["text"]


def test_status_line_is_masked():
    s = make()
    tok = "sk-ant-api03-abcdefGHIJ123456"
    ev = {"kind": "tool", "name": "Bash", "detail": f"curl -H 'x: {tok}' http://a"}
    script_run(s, "ok", on_events=[ev], stats=stats())
    s.g["edit_interval_seconds"] = 0
    bot = run_task(s)
    assert bot.edits and all(tok not in e for e in bot.edits)
    assert "[REDACTED]" in " ".join(bot.edits)


def test_media_failure_message_is_masked():
    s = make()
    tok = "ghp_" + "d2" * 18
    calls = []

    class M:
        text = caption = None
        chat_id, message_id = 5, 1
        voice = audio = document = None
        photo = [SimpleNamespace(file_id="f")]

        async def reply_text(self, t, **kw):
            calls.append(t)

    async def boom(*a, **k):
        raise RuntimeError(f"download failed {tok}")
    s.download = boom
    upd = SimpleNamespace(effective_message=M(), effective_chat=SimpleNamespace(id=5))
    assert asyncio.run(s.build_prompt(upd, None, "")) is None
    assert calls and tok not in calls[0] and "[REDACTED]" in calls[0]


def test_log_filter_masks_traceback_and_message(tmp_path):
    import logging
    tok = "ghp_" + "e3" * 18
    f = bridge.Redact(["exactsecret1234"])
    try:
        raise RuntimeError(f"bad {tok}")
    except RuntimeError:
        import sys
        rec = logging.LogRecord("x", logging.ERROR, "p", 1, "m exactsecret1234 %s", (tok,), sys.exc_info())
    f.filter(rec)
    out = logging.Formatter("%(message)s").format(rec)
    assert tok not in out and "exactsecret1234" not in out


# ---- idle reset: a big session idle past the prompt-cache lifetime starts fresh, /back returns to it
def test_idle_reset_new_session_and_back(tmp_path):
    s = make(store=StateStore(tmp_path / "s.json"))
    s.cfg = {**s.cfg, "idle_new_session_minutes": 60, "idle_new_session_tokens": 30000}
    now = [1_000_000.0]
    s.wall = lambda: now[0]
    seen = []

    def run_with(sid, tokens):
        async def run(prompt, session_id=None, on_event=None):
            seen.append(session_id)
            s.runner.stats = stats(tokens=tokens)
            return "ok", session_id or sid, False
        s.runner.run = run

    run_with("old", 85000)
    run_task(s)
    assert s.sessions[5] == "old" and s.ctx[5]["last"] == now[0]
    now[0] += 59 * 60                     # still cached: resume
    run_task(s)
    assert seen[-1] == "old"
    now[0] += 3 * 3600                    # cache gone: fresh session with a notice
    run_with("new", 20000)
    bot = run_task(s)
    assert seen[-1] is None and s.sessions[5] == "new"
    assert any(t.startswith("🆕 New session: idle 3 h and the old one (~85k tokens)") for t in bot.texts())
    assert StateStore(tmp_path / "s.json").load("claude")["ctx"][5]["last"] == now[0]   # persisted

    # /back survives a bridge restart: a fresh service on the same state file still has the parked session
    s2 = make(store=StateStore(tmp_path / "s.json"))
    assert s2.parked[5]["sid"] == "old" and s2.parked[5]["ctx"]["tokens"] == 85000
    replies = []

    async def back():
        msg = SimpleNamespace(reply_text=lambda t, **kw: asyncio.sleep(0, replies.append(t)))
        await s.command(SimpleNamespace(effective_message=msg), "back", "", 5, 5)
    asyncio.run(back())
    assert s.sessions[5] == "old" and s.ctx[5]["tokens"] == 85000 and replies[-1].startswith("↩️ Back")
    run_task(s)                           # /back refreshed the idle clock: no second reset
    assert seen[-1] == "old"
    asyncio.run(back())
    assert replies[-1] == "Nothing to go back to."


def test_idle_reset_skips_small_sessions_resumed_tasks_and_off():
    s = make()
    s.cfg = {**s.cfg, "idle_new_session_minutes": 60, "idle_new_session_tokens": 30000}
    s.wall = lambda: 10_000.0
    s.sessions[5] = "sid"
    s.ctx[5] = {**s.ctx_entry(5), "tokens": 20000, "last": 0.0}
    assert s.idle_reset_due(5) is None    # small: re-reading is cheap
    s.ctx[5]["tokens"] = 90000
    assert s.idle_reset_due(5) == 10_000.0
    s.ctx[5].pop("last")
    assert s.idle_reset_due(5) is None    # no timestamp (state from before this feature)
    s.ctx[5]["last"] = 0.0
    s.cfg["idle_new_session_minutes"] = 0
    assert s.idle_reset_due(5) is None    # off
    s.cfg["idle_new_session_minutes"] = 60
    seen = []

    async def run(prompt, session_id=None, on_event=None):
        seen.append(session_id)
        s.runner.stats = stats()
        return "ok", session_id, False
    s.runner.run = run
    asyncio.run(s.run_task(5, "go on", 7, session_id="sid"))   # /resume after a restart keeps its session
    assert seen == ["sid"] and 5 not in s.parked
