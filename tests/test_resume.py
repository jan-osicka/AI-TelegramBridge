"""Resume after restart: in-flight journal, queue mirror, startup offer, /resume and /drop."""
import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pyotp

from telegram_bridge import bridge, settings
from telegram_bridge.auth import Auth
from telegram_bridge.bridge import BotService
from telegram_bridge.journal import Journal
from telegram_bridge.runner import RunStats
from telegram_bridge.state import StateStore

SECRET = pyotp.random_base32()


class Clock:
    t = 1_700_000_000.0

    def __call__(self):
        return self.t


class Status:
    async def edit_text(self, text):
        pass

    async def delete(self):
        pass


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat, text, **kw):
        self.sent.append((chat, text))
        return Status()

    async def send_chat_action(self, chat, action):
        pass


def make(tmp_path, unlocked=True, store=None, bot="claude"):
    c = Clock()
    auth = Auth([5], SECRET, clock=c)
    if unlocked:
        assert auth.unlock(5, pyotp.TOTP(SECRET).at(c.t))[0]
    cfg = settings.load_config()
    s = BotService(bot, cfg["bots"][bot], "t", auth, cfg["general"], clock=c, store=store,
                   journal=Journal(tmp_path / "inflight.json"))
    s.app = SimpleNamespace(bot=FakeBot())
    return s


def data(tmp_path):
    p = tmp_path / "inflight.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"bots": {}}


def rec(prompt="do the thing", **kw):
    r = {"chat": 5, "uid": 5, "mode": "agent", "prompt": prompt, "text": "", "user_text": prompt, "images": [],
         "session_id": "sid-old", "started": datetime.now().isoformat(timespec="seconds"), "msg_id": 9,
         "jid": prompt, "save": False, "resume_count": 0}
    return {**r, **kw}


def script(s, seen, final="done", err=False, hang=False):
    async def run(prompt, session_id=None, on_event=None):
        seen.append((prompt, session_id))
        seen.append(dict(s.journal._read()["bots"]))   # snapshot of the journal while running
        s.runner.stats = RunStats()
        if hang:
            await asyncio.sleep(60)
        return final, "sid-1", err
    s.runner.run = run


def run_work(s, items, seen, after=None):
    async def go():
        for it in items:
            await s.enqueue(5, 5, it)
        t = asyncio.create_task(s.work())
        await asyncio.sleep(0.2)
        if after:
            await after()
        t.cancel()
    asyncio.run(go())


def agent(prompt="hello", **kw):
    return {"mode": "agent", "prompt": prompt, "text": "", "images": [], "msg_id": 3, **kw}


# ---- journal while running
def test_journal_written_before_run_and_cleared_after_success(tmp_path):
    s = make(tmp_path)
    s.sessions[5] = "sid-old"
    seen = []
    script(s, seen)
    run_work(s, [agent("fix the bug")], seen)
    running = seen[1]["claude"]["5"]["running"]
    assert running["prompt"] == "fix the bug" and running["session_id"] == "sid-old" and running["msg_id"] == 3
    assert running["mode"] == "agent" and running["uid"] == 5 and running["user_text"] == "fix the bug"
    assert data(tmp_path) == {"bots": {}}


def test_journal_cleared_after_error_and_timeout(tmp_path):
    for err, timed_out in ((True, False), (False, True)):
        s = make(tmp_path)
        s.runner.timed_out = timed_out
        seen = []
        script(s, seen, final="boom", err=err)
        run_work(s, [agent()], seen)
        assert data(tmp_path) == {"bots": {}}


def test_journal_cleared_after_stop_command(tmp_path):
    s = make(tmp_path)
    s.runner.stopped = True
    seen = []
    script(s, seen, final="stopped", err=True)
    run_work(s, [agent()], seen)
    assert data(tmp_path) == {"bots": {}}


def test_graceful_shutdown_keeps_record(tmp_path):
    s = make(tmp_path)
    seen = []
    script(s, seen, hang=True)

    async def after():
        await s.stop()
    run_work(s, [agent("long job")], seen, after)
    assert data(tmp_path)["bots"]["claude"]["5"]["running"]["prompt"] == "long job"


def test_restart_notice_mentions_resume():
    assert "/resume" in bridge.RESTART_NOTICE


def test_background_save_run_is_journaled(tmp_path):
    s = make(tmp_path)
    seen = []
    script(s, seen)
    run_work(s, [{"mode": "agent", "prompt": "save notes", "text": "", "images": [], "save": True}], seen)
    assert seen[1]["claude"]["5"]["running"]["save"] is True


# ---- queue mirror
def test_queue_mirrored_and_removed_when_taken(tmp_path):
    s = make(tmp_path)

    async def go():
        await s.enqueue(5, 5, agent("one"))
        await s.enqueue(5, 5, agent("two", images=[tmp_path / "a.jpg"]))
    asyncio.run(go())
    q = data(tmp_path)["bots"]["claude"]["5"]["queue"]
    assert [r["prompt"] for r in q] == ["one", "two"] and q[1]["images"] == [str(tmp_path / "a.jpg")]
    seen = []
    script(s, seen, hang=True)

    async def go2():
        t = asyncio.create_task(s.work())
        await asyncio.sleep(0.2)
        s.shutting_down = True
        t.cancel()
    asyncio.run(go2())
    e = data(tmp_path)["bots"]["claude"]["5"]
    assert e["running"]["prompt"] == "one" and [r["prompt"] for r in e["queue"]] == ["two"]


# ---- startup offer
def test_startup_message_with_queue_and_twice(tmp_path):
    j = Journal(tmp_path / "inflight.json")
    j.start("claude", 5, rec("write the report", resume_count=1))
    j.queue_add("claude", 5, rec("second " + "x" * 100, jid="q1"))
    s = make(tmp_path)
    asyncio.run(s.offer_resume())
    (chat, text), = s.app.bot.sent
    started = datetime.fromisoformat(rec()["started"])
    assert chat == 5
    assert "Restarted while I was working on: “write the report”" in text
    assert f"(started {started:%H:%M})" in text and "(interrupted twice)" in text
    assert "/resume" in text and "/drop" in text
    assert "Also waiting: 1 queued message(s)." in text and ("- second " + "x" * 53) in text
    assert ("- second " + "x" * 54) not in text


def test_startup_message_once_and_not_twice_for_first_interruption(tmp_path):
    Journal(tmp_path / "inflight.json").start("claude", 5, rec("job"))
    s = make(tmp_path)
    asyncio.run(s.offer_resume())
    assert len(s.app.bot.sent) == 1 and "interrupted twice" not in s.app.bot.sent[0][1]
    assert "Also waiting" not in s.app.bot.sent[0][1]


def test_startup_message_sent_even_when_locked_and_only_to_allowed(tmp_path):
    j = Journal(tmp_path / "inflight.json")
    j.start("claude", 5, rec("job"))
    j.start("claude", 77, rec("other", chat=77))
    s = make(tmp_path, unlocked=False)
    asyncio.run(s.offer_resume())
    assert [c for c, _ in s.app.bot.sent] == [5]


def test_other_bot_records_are_left_alone(tmp_path):
    Journal(tmp_path / "inflight.json").start("qwen", 5, rec("job"))
    s = make(tmp_path)
    asyncio.run(s.offer_resume())
    assert s.app.bot.sent == []


def test_old_records_dropped_silently(tmp_path):
    old = (datetime.now() - timedelta(days=8)).isoformat(timespec="seconds")
    Journal(tmp_path / "inflight.json").start("claude", 5, rec("ancient", started=old))
    s = make(tmp_path)
    asyncio.run(s.offer_resume())
    assert s.app.bot.sent == [] and data(tmp_path) == {"bots": {}}


def test_corrupt_file_is_empty_and_logged(tmp_path, caplog):
    (tmp_path / "inflight.json").write_text("{broken", encoding="utf-8")
    s = make(tmp_path)
    asyncio.run(s.offer_resume())
    assert s.app.bot.sent == [] and "corrupt" in caplog.text
    asyncio.run(s.enqueue(5, 5, agent("fresh")))   # and it recovers by overwriting
    assert data(tmp_path)["bots"]["claude"]["5"]["queue"][0]["prompt"] == "fresh"


# ---- commands
def send(s, text):
    m = SimpleNamespace(text=text, caption=None, chat_id=5, texts=[])

    async def reply_text(t, **kw):
        m.texts.append(t)

    async def delete():
        pass
    m.reply_text, m.delete = reply_text, delete
    asyncio.run(s.on_update(SimpleNamespace(effective_message=m, effective_user=SimpleNamespace(id=5)), None))
    return m.texts


def test_resume_reruns_in_same_session_with_prefix_then_queue(tmp_path):
    store = StateStore(tmp_path / "s.json")
    store.save("claude", {5: "sid-latest"}, {}, {})
    j = Journal(tmp_path / "inflight.json")
    j.start("claude", 5, rec("push the release"))
    j.queue_add("claude", 5, rec("next one", jid="q1"))
    s = make(tmp_path, store=store)
    asyncio.run(s.offer_resume())
    assert send(s, "/resume")[0].startswith("Resuming")
    seen = []
    script(s, seen)
    async def go():
        t = asyncio.create_task(s.work())
        await asyncio.sleep(0.3)
        t.cancel()
    asyncio.run(go())
    prompts = [x for x in seen if isinstance(x, tuple)]
    assert prompts[0] == (bridge.RESUME_PREFIX + "push the release", "sid-old")   # recorded session wins over the later one
    assert prompts[1] == ("next one", "sid-1") or prompts[1][0] == "next one"
    assert data(tmp_path) == {"bots": {}}
    assert send(s, "/resume") == ["Nothing to resume."]


def test_resumed_task_interrupted_again_counts_twice_without_double_prefix(tmp_path):
    Journal(tmp_path / "inflight.json").start("claude", 5, rec("job"))
    s = make(tmp_path)
    asyncio.run(s.offer_resume())
    send(s, "/resume")
    seen = []
    script(s, seen, hang=True)

    async def after():
        await s.stop()
    async def go():
        t = asyncio.create_task(s.work())
        await asyncio.sleep(0.2)
        await after()
        t.cancel()
    asyncio.run(go())
    r = data(tmp_path)["bots"]["claude"]["5"]["running"]
    assert r["resume_count"] == 1 and r["prompt"] == "job"
    s2 = make(tmp_path)
    asyncio.run(s2.offer_resume())
    assert "(interrupted twice)" in s2.app.bot.sent[0][1]


def test_resume_chat_mode_record_is_asked_as_normal(tmp_path):
    Journal(tmp_path / "inflight.json").start("qwen", 5, rec("what is 2+2", mode="chat", text="what is 2+2"))
    s = make(tmp_path, bot="qwen")
    asyncio.run(s.offer_resume())
    send(s, "/resume")
    chat, uid, item = s.queue.get_nowait()
    assert item["mode"] == "chat" and item["text"] == "what is 2+2" and item["prompt"] == "what is 2+2"


def test_resume_refused_while_locked(tmp_path):
    Journal(tmp_path / "inflight.json").start("claude", 5, rec("job"))
    s = make(tmp_path, unlocked=False)
    asyncio.run(s.offer_resume())
    assert send(s, "/resume") == [bridge.LOCKED_NOTICE]
    assert s.queue.empty() and s.journal.pending("claude", 5)["task"]["prompt"] == "job"


def test_drop_clears_record_and_queue(tmp_path):
    j = Journal(tmp_path / "inflight.json")
    j.start("claude", 5, rec("job"))
    j.queue_add("claude", 5, rec("q", jid="q1"))
    s = make(tmp_path)
    asyncio.run(s.offer_resume())
    assert send(s, "/drop") == ["Dropped."]
    assert data(tmp_path) == {"bots": {}} and send(s, "/resume") == ["Nothing to resume."]


def test_nothing_to_resume_and_commands_listed(tmp_path):
    s = make(tmp_path)
    assert send(s, "/resume") == ["Nothing to resume."]
    names = [c for c, _ in bridge.COMMANDS]
    assert "resume" in names and "drop" in names


def test_stop_all_clears_mirrored_queue(tmp_path):
    s = make(tmp_path)
    asyncio.run(s.enqueue(5, 5, agent("a")))
    send(s, "/stop all")
    assert data(tmp_path) == {"bots": {}}


# ---- fix pass
def test_task_finishing_during_shutdown_window_clears_record(tmp_path):
    s = make(tmp_path)
    seen = []
    script(s, seen)

    async def after():
        s.shutting_down = True   # stop() began, but the runner was idle: no kill
    run_work(s, [agent("quick")], seen, after)
    assert data(tmp_path) == {"bots": {}}


def test_shutdown_kill_keeps_record(tmp_path, monkeypatch):
    s = make(tmp_path)
    seen = []
    script(s, seen, hang=True)
    monkeypatch.setattr(type(s.runner), "running", property(lambda self: True))
    monkeypatch.setattr(type(s.runner), "stop", lambda self: True)

    async def after():
        await s.stop()
    run_work(s, [agent("long job")], seen, after)
    assert data(tmp_path)["bots"]["claude"]["5"]["running"]["prompt"] == "long job"


def test_worker_does_not_start_queued_item_after_shutdown(tmp_path):
    s = make(tmp_path)
    s.shutting_down = True
    seen = []
    script(s, seen)
    run_work(s, [agent("later")], seen)
    assert not seen
    assert data(tmp_path)["bots"]["claude"]["5"]["queue"][0]["prompt"] == "later"


def test_resume_busy_does_nothing(tmp_path):
    Journal(tmp_path / "inflight.json").start("claude", 5, rec("job"))
    s = make(tmp_path)
    asyncio.run(s.offer_resume())
    asyncio.run(s.enqueue(5, 5, agent("running now")))
    assert send(s, "/resume") == ["Busy: wait until the current task finishes, then send /resume again."]
    assert s.journal.pending("claude", 5)["task"]["prompt"] == "job"


def test_recover_merges_existing_pending(tmp_path):
    j = Journal(tmp_path / "inflight.json")
    j.start("claude", 5, rec("old task", resume_count=1))
    j.queue_add("claude", 5, rec("old q", jid="oq"))
    j.recover("claude")
    j.start("claude", 5, rec("new task", jid="n"))
    j.queue_add("claude", 5, rec("new q", jid="nq"))
    p = j.recover("claude")[5]
    assert p["task"]["prompt"] == "old task" and p["task"]["resume_count"] == 1
    assert [r["prompt"] for r in p["queue"]] == ["old q", "new task", "new q"]


def test_garbled_timestamp_is_expired(tmp_path):
    j = Journal(tmp_path / "inflight.json")
    j.start("claude", 5, rec("job", started="garbage"))
    assert j.recover("claude") == {}


def test_write_fsyncs(tmp_path, monkeypatch):
    import os
    calls = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (calls.append(fd), real(fd)))
    Journal(tmp_path / "inflight.json").start("claude", 5, rec("job"))
    assert calls


def test_offer_sent_once(tmp_path):
    Journal(tmp_path / "inflight.json").start("claude", 5, rec("job"))
    s = make(tmp_path)
    asyncio.run(s.offer_resume())
    asyncio.run(s.offer_resume())
    assert len(s.app.bot.sent) == 1


def test_save_followup_not_mirrored_and_prompt_capped(tmp_path):
    s = make(tmp_path)
    asyncio.run(s.enqueue(5, 5, {**agent("save"), "save": True}))
    assert data(tmp_path) == {"bots": {}}
    asyncio.run(s.enqueue(5, 5, agent("x" * 30000)))
    assert len(data(tmp_path)["bots"]["claude"]["5"]["queue"][0]["prompt"]) == 20000
