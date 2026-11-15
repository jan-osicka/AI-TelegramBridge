"""Pass 1: persisted state, task timeout, stdin prompt, inbox cleanup, /stop all, idle lock, cwd roots."""
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pyotp

from telegram_bridge import housekeeping
from telegram_bridge import settings
from telegram_bridge.auth import Auth
from telegram_bridge.bridge import BotService
from telegram_bridge.runner import Runner, build_cmd, uses_stdin
from telegram_bridge.state import StateStore

SECRET = pyotp.random_base32()
PY = sys.executable


class Clock:
    def __init__(self, t=1_700_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


# ---- persistence
def test_state_roundtrip_atomic_and_keyed_by_bot(tmp_path):
    st = StateStore(tmp_path / "sessions.json")
    st.save("claude", {5: "sid-1"}, {5: "C:\\AI"}, {})
    st.save("qwen", {5: "sid-2"}, {}, {5: [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]})
    a, b = st.load("claude"), st.load("qwen")
    assert a["sessions"] == {5: "sid-1"} and a["cwds"] == {5: "C:\\AI"} and a["chats"] == {}
    assert b["sessions"] == {5: "sid-2"} and b["chats"][5][1]["content"] == "yo"
    assert not list(tmp_path.glob("*.tmp"))
    assert st.load("nobody") == {"sessions": {}, "cwds": {}, "chats": {}, "ctx": {}, "parked": {}}


def test_state_corrupt_starts_empty(tmp_path, caplog):
    p = tmp_path / "sessions.json"
    p.write_text("{broken")
    st = StateStore(p)
    assert st.load("claude")["sessions"] == {}
    assert "corrupt" in caplog.text
    st.save("claude", {1: "x"}, {}, {})   # recovers by overwriting
    assert st.load("claude")["sessions"] == {1: "x"}


def test_state_never_stores_images(tmp_path):
    st = StateStore(tmp_path / "s.json")
    hist = {1: [{"role": "user", "content": [{"type": "text", "text": "look"},
                                             {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}]}
    st.save("qwen", {}, {}, hist)
    raw = (tmp_path / "s.json").read_text()
    assert "base64" not in raw and "AAAA" not in raw and "[image]" in raw


def svc(name, store=None, auth=None, **general):
    cfg = settings.load_config()
    g = {**cfg["general"], **general}
    return BotService(name, cfg["bots"][name], "t", auth or Auth([42], SECRET), g, clock=Clock(0.0), store=store)


def test_botservice_restores_and_saves(tmp_path):
    store = StateStore(tmp_path / "s.json")
    store.save("qwen", {7: "sess"}, {7: str(tmp_path)},
               {7: [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]})
    s = svc("qwen", store, allowed_cwd_roots=[str(tmp_path)])
    assert s.sessions == {7: "sess"} and s.cwds == {7: str(tmp_path)} and len(s.chats.history[7]) == 2
    s.sessions[8] = "other"
    s.save_state()
    assert StateStore(tmp_path / "s.json").load("qwen")["sessions"] == {7: "sess", 8: "other"}
    assert svc("claude", store).sessions == {}   # the other bot is separate


def test_restored_cwd_outside_roots_is_dropped(tmp_path):
    store = StateStore(tmp_path / "s.json")
    store.save("claude", {}, {1: str(tmp_path)}, {})
    assert svc("claude", store, allowed_cwd_roots=[str(tmp_path / "other")]).cwds == {}


# ---- command handling helpers
class FakeMsg:
    def __init__(self, text, chat_id=5):
        self.text, self.caption, self.chat_id = text, None, chat_id
        self.texts = []

    async def reply_text(self, t, **kw):
        self.texts.append(t)

    async def delete(self):
        pass


def upd(text, uid=42, chat_id=5):
    m = FakeMsg(text, chat_id)
    return SimpleNamespace(effective_message=m, effective_user=SimpleNamespace(id=uid)), m


def unlocked_auth(clock=None, **kw):
    clock = clock or Clock()
    a = Auth([42], SECRET, clock=clock, **kw)
    assert a.unlock(42, pyotp.TOTP(SECRET).at(clock.t))[0]
    return a


def send(s, text):
    u, m = upd(text)
    asyncio.run(s.on_update(u, None))
    return m.texts


def test_exit_and_cwd_persist(tmp_path):
    store = StateStore(tmp_path / "s.json")
    sub = tmp_path / "proj"
    sub.mkdir()
    s = svc("claude", store, unlocked_auth(), allowed_cwd_roots=[str(tmp_path)])
    s.sessions[5] = "abc"
    s.save_state()
    assert send(s, f"/cwd {sub}")[0].startswith("cwd set")
    st = store.load("claude")
    assert st["cwds"] == {5: str(sub.resolve())} and st["sessions"] == {}   # /cwd resets the session
    s.sessions[5] = "def"
    s.save_state()
    send(s, "/exit")
    assert store.load("claude")["sessions"] == {}


def test_cwd_outside_roots_refused(tmp_path):
    allowed = tmp_path / "ok"
    other = tmp_path / "other"
    allowed.mkdir()
    other.mkdir()
    s = svc("claude", None, unlocked_auth(), allowed_cwd_roots=[str(allowed)])
    assert send(s, f"/cwd {other}") == ["Not allowed."]
    assert 5 not in s.cwds
    assert send(s, f"/cwd {allowed}")[0].startswith("cwd set")


def test_cwd_allowed_rules(tmp_path):
    root = tmp_path / "Root"
    (root / "a" / "b").mkdir(parents=True)
    (tmp_path / "Root2").mkdir()
    assert housekeeping.cwd_allowed(root, [root])
    assert housekeeping.cwd_allowed(root / "a" / "b", [root])
    assert housekeeping.cwd_allowed(str(root).upper(), [str(root).lower()]) == (os.name == "nt")
    assert not housekeeping.cwd_allowed(tmp_path / "Root2", [root])          # sibling with same prefix
    assert not housekeeping.cwd_allowed(root / ".." / "Root2", [root])       # traversal
    assert not housekeeping.cwd_allowed(tmp_path, [root])                     # parent


# ---- /stop all
def test_stop_all_clears_queue_and_plain_stop_mentions_it():
    s = svc("claude", None, unlocked_auth())

    async def go():
        for i in range(3):
            await s.queue.put((5, 42, {"mode": "agent", "prompt": str(i), "text": "", "images": []}))
        u, m = upd("/stop")
        await s.on_update(u, None)
        assert "/stop all" in m.texts[0] and s.queue.qsize() == 3
        u, m = upd("/stop all")
        await s.on_update(u, None)
        assert "Cleared 3" in m.texts[0] and s.queue.qsize() == 0
    asyncio.run(go())
    assert "stop all" in s.help().lower()


# ---- idle lock
def test_idle_lock_after_inactivity_and_activity_resets():
    c = Clock()
    a = unlocked_auth(c, idle_lock_hours=3)
    c.t += 2 * 3600
    assert a.is_unlocked(42)
    a.touch(42)
    c.t += 2 * 3600          # 4 h since unlock, 2 h since activity
    assert a.is_unlocked(42)
    c.t += 3600 + 1          # 3 h since activity
    assert not a.is_unlocked(42)
    assert a.expiry(42) is None


def test_idle_lock_off_and_hard_cap_still_applies():
    c = Clock()
    a = unlocked_auth(c, idle_lock_hours=0, session_hours=12)
    c.t += 11 * 3600
    assert a.is_unlocked(42)
    c = Clock()
    a = unlocked_auth(c, idle_lock_hours=3, session_hours=12)
    for _ in range(5):       # active every 2.9 h: idle never trips, but the 12 h cap does
        c.t += 2.9 * 3600
        if a.is_unlocked(42):
            a.touch(42)
    assert not a.is_unlocked(42)


def test_idle_activity_persists_across_restart(tmp_path):
    p, c = tmp_path / "state.json", Clock()
    a = Auth([42], SECRET, clock=c, state_path=p, idle_lock_hours=3)
    a.unlock(42, pyotp.TOTP(SECRET).at(c.t))
    c.t += 2 * 3600
    a.touch(42)
    a._save()
    assert "activity" in json.loads(p.read_text())
    c.t += 2 * 3600
    assert Auth([42], SECRET, clock=c, state_path=p, idle_lock_hours=3).is_unlocked(42)
    c.t += 3600
    assert not Auth([42], SECRET, clock=c, state_path=p, idle_lock_hours=3).is_unlocked(42)


def test_message_counts_as_activity_in_bridge():
    c = Clock()
    a = unlocked_auth(c, idle_lock_hours=3)
    s = svc("claude", None, a)
    c.t += 2 * 3600
    send(s, "/status")
    c.t += 2 * 3600
    assert a.is_unlocked(42)


# ---- inbox cleanup
def test_cleanup_inbox_deletes_only_old_files(tmp_path):
    old, new = tmp_path / "old.jpg", tmp_path / "new.jpg"
    old.write_text("x")
    new.write_text("y")
    past = time.time() - 8 * 86400
    os.utime(old, (past, past))
    assert housekeeping.cleanup_inbox(tmp_path, 7) == 1
    assert not old.exists() and new.exists()
    assert housekeeping.cleanup_inbox(tmp_path, 0) == 0
    assert housekeeping.cleanup_inbox(tmp_path / "missing", 7) == 0


def test_voice_file_deleted_after_transcription(tmp_path, monkeypatch):
    from telegram_bridge import bridge
    f = tmp_path / "v.ogg"
    f.write_text("audio")
    s = svc("claude", None, unlocked_auth())

    async def download(ctx, fid, name):
        return f

    async def transcribe(path, model):
        return "hello"
    monkeypatch.setattr(s, "download", download)
    monkeypatch.setattr(bridge.media, "transcribe", transcribe)

    class Msg(FakeMsg):
        voice = SimpleNamespace(file_id="1", file_name=None)
        audio = photo = document = None

        async def reply_text(self, t, **kw):
            self.texts.append(t)
            return SimpleNamespace(delete=self.delete)
    item = asyncio.run(s.build_prompt(SimpleNamespace(effective_message=Msg(None)), None, ""))
    assert item["prompt"] == "hello" and not f.exists()


# ---- runner: stdin and timeout
def test_uses_stdin_and_build_cmd():
    cfg = settings.load_config()["bots"]
    assert uses_stdin(cfg["claude"]) and uses_stdin(cfg["qwen"])
    assert "{prompt}" not in " ".join(cfg["claude"]["args"])
    assert uses_stdin({"args": ["-p"]}) and not uses_stdin({"args": ["-p", "{prompt}"]})
    assert build_cmd({"command": "c", "args": ["-p"]}, "very secret prompt") == ["c", "-p"]


ECHO = ("import sys,json;p=sys.stdin.buffer.read().decode('utf-8');"
        "print(json.dumps({'type':'result','result':p,'session_id':'s1','is_error':False}),flush=True)")


def test_runner_sends_prompt_on_stdin_utf8_and_large():
    r = Runner({"command": PY, "args": ["-c", ECHO]}, ".")
    prompt = "Příliš žluťoučký kůň " + "x" * 200_000
    final, sid, err = asyncio.run(r.run(prompt))
    assert final == prompt and sid == "s1" and not err


def test_runner_timeout_kills_process():
    r = Runner({"command": PY, "args": ["-c", "import time;time.sleep(60)"]}, ".", timeout_s=0.5)
    t0 = time.monotonic()
    final, sid, err = asyncio.run(r.run("hi"))
    assert time.monotonic() - t0 < 15
    assert err and final.startswith("Task timed out after ") and final.endswith(" min and was stopped.")
    assert r.timed_out and not r.running


def test_timeout_message_uses_whole_minutes():
    r = Runner({"command": PY, "args": ["-c", "import time;time.sleep(60)"]}, ".", timeout_s=0.3)
    r.timeout_s = 0.3
    assert asyncio.run(r.run("x"))[0].startswith("Task timed out")
    r2 = Runner({"command": PY, "args": ["-c", ECHO]}, ".", timeout_s=60)
    assert not asyncio.run(r2.run("ok"))[2]   # finishing in time is not an error


def test_botservice_timeout_from_config():
    assert svc("claude", None, task_timeout_minutes=60).runner.timeout_s == 3600
    assert svc("claude", None, task_timeout_minutes=0).runner.timeout_s is None
