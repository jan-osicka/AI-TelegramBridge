"""Nothing gets lost: several results per run, answers kept until Telegram accepts them, no silent drops, edits."""
import asyncio
import json
import sys
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pyotp
import pytest
from telegram.error import BadRequest, Forbidden, NetworkError, TimedOut

from telegram_bridge import bridge, runner, settings
from telegram_bridge.auth import Auth
from telegram_bridge.bridge import BotService
from telegram_bridge.journal import Journal
from telegram_bridge.runner import Runner

SECRET = pyotp.random_base32()


class Status:
    async def edit_text(self, text):
        pass

    async def delete(self):
        pass


class FakeBot:
    def __init__(self):
        self.sent, self.docs, self.fail = [], [], []   # fail: exceptions for the next send_message calls (None = ok)

    async def send_message(self, chat, text, **kw):
        if self.fail and (e := self.fail.pop(0)):
            raise e
        self.sent.append(text)
        return Status()

    async def send_document(self, chat, f, filename=None, **kw):
        self.docs.append(f.read().decode())


def make(tmp_path, **general):
    cfg = settings.load_config()
    auth = Auth([5], SECRET)
    auth.unlock(5, pyotp.TOTP(SECRET).now())
    s = BotService("claude", cfg["bots"]["claude"], "t", auth, {**cfg["general"], **general},
                   journal=Journal(tmp_path / "inflight.json"))
    s.app = SimpleNamespace(bot=FakeBot())
    return s


# ---- several results in one run
def result(text):
    return {"type": "result", "result": text, "session_id": "s1", "is_error": False}


def proc(steps):
    """Runner whose process prints the events; a number means sleep that long."""
    code = ("import time,json\nfor s in json.loads(" + repr(json.dumps(steps)) + "):\n"
            "  time.sleep(s) if isinstance(s,(int,float)) else print(json.dumps(s),flush=True)")
    return Runner({"command": sys.executable, "args": ["-c", code]}, ".")


def collect(r):
    got = []

    async def on_event(ev):
        if ev["kind"] == "answer":
            got.append((ev["text"], time.monotonic()))
    out = asyncio.run(r.run("x", None, on_event))
    return out, got, time.monotonic()


def test_second_result_does_not_replace_the_first():
    out, got, _ = collect(proc([result("STARTED"), result("FINISHED")]))
    assert [t for t, _ in got] == ["STARTED"] and out == ("FINISHED", "s1", False)


def test_answer_is_delivered_while_background_work_runs(monkeypatch):
    monkeypatch.setattr(runner, "ANSWER_GRACE_S", 0.2)
    out, got, end = collect(proc([result("STARTED"), 2, result("FINISHED")]))
    assert got[0][0] == "STARTED" and end - got[0][1] > 1
    assert out == ("FINISHED", "s1", False)


def test_nothing_left_after_an_early_answer(monkeypatch):
    monkeypatch.setattr(runner, "ANSWER_GRACE_S", 0.2)
    out, got, _ = collect(proc([result("ONLY"), 1]))
    assert len(got) == 1 and out == (None, "s1", False)


def test_single_result_is_the_final_answer():
    out, got, _ = collect(proc([result("hi")]))
    assert out[0] == "hi" and got == []


def test_subagent_text_is_never_the_fallback_answer():
    def text(t, parent=None):
        return {"type": "assistant", "parent_tool_use_id": parent, "message": {"content": [{"type": "text", "text": t}]}}
    out, _, _ = collect(proc([text("main"), text("sub", "toolu_1")]))
    assert out[0] == "main" and out[2]


def test_bridge_sends_both_answers_and_keeps_the_session(tmp_path):
    s = make(tmp_path)

    async def run(prompt, session_id=None, on_event=None):
        await on_event({"kind": "answer", "text": "STARTED", "session_id": "s1", "is_error": False})
        assert s.sessions[5] == "s1"   # already resumable if the rest of the run is killed
        return "FINISHED", "s1", False
    s.runner.run = run
    asyncio.run(s.run_task(5, "hi"))
    assert s.app.bot.sent[1:] == ["STARTED", "FINISHED"]


def test_bridge_sends_nothing_more_when_all_was_delivered(tmp_path):
    s = make(tmp_path)

    async def run(prompt, session_id=None, on_event=None):
        await on_event({"kind": "answer", "text": "ONLY", "session_id": "s1", "is_error": False})
        return None, "s1", False
    s.runner.run = run
    asyncio.run(s.run_task(5, "hi"))
    assert s.app.bot.sent[1:] == ["ONLY"]


# ---- answers survive a failed send
@pytest.mark.parametrize("err", [NetworkError("down"), TimedOut()])
def test_failed_answer_is_sent_after_a_restart(tmp_path, err):
    s = make(tmp_path)
    s.app.bot.fail = [err]
    asyncio.run(s.send_answer(5, "the **answer**", footer="· 5% context"))
    assert s.app.bot.sent == []
    s2 = make(tmp_path)
    s2.journal.recover("claude")   # what start() does
    asyncio.run(s2.resend_unsent())
    assert s2.app.bot.sent == ["the <b>answer</b>\n\n· 5% context"]
    asyncio.run(s2.resend_unsent())
    assert len(s2.app.bot.sent) == 1 and s2.journal._read() == {"bots": {}}


def test_retry_sends_only_the_missing_parts(tmp_path):
    s = make(tmp_path, document_threshold_chars=100_000)
    s.app.bot.fail = [None, NetworkError("down")]
    asyncio.run(s.send_answer(5, "\n".join(f"line {i}" for i in range(1500))))
    assert len(s.app.bot.sent) == 1
    asyncio.run(s.resend_unsent())
    lines = [ln for t in s.app.bot.sent for ln in t.split("\n") if ln]
    assert lines == [f"line {i}" for i in range(1500)]


def test_long_answer_file_is_not_sent_twice(tmp_path):
    s = make(tmp_path, document_threshold_chars=10)
    s.app.bot.fail = [None, NetworkError("down")]   # preview ok, file ok, footer fails
    asyncio.run(s.send_answer(5, "x" * 50, footer="foot"))
    asyncio.run(s.resend_unsent())
    assert s.app.bot.docs == ["x" * 50] and s.app.bot.sent[-1] == "foot"


@pytest.mark.parametrize("errs", [[BadRequest("bad"), BadRequest("bad")], [Forbidden("blocked")]])
def test_permanent_errors_raise_and_are_not_kept(tmp_path, errs):
    s = make(tmp_path)
    s.app.bot.fail = list(errs)
    with pytest.raises(type(errs[0])):
        asyncio.run(s.send_answer(5, "x"))
    assert s.journal.unsent("claude") == []


def test_expired_and_foreign_answers_are_not_sent(tmp_path):
    s = make(tmp_path)
    old = (datetime.now() - timedelta(days=8)).isoformat(timespec="seconds")
    base = {"text": "t", "hidden": False, "reply_to": None, "footer": "", "done": 0}
    s.journal.unsent_put("claude", 5, {**base, "id": "a", "started": old})
    s.journal.unsent_put("claude", 99, {**base, "id": "b", "started": datetime.now().isoformat()})
    asyncio.run(s.resend_unsent())
    assert s.app.bot.sent == [] and [r["id"] for _, r in s.journal.unsent("claude")] == ["b"]


def test_saved_answer_is_masked_on_disk(tmp_path):
    s = make(tmp_path)
    s.app.bot.fail = [NetworkError("down")]
    token = "123456789:" + "AbCdEfGhIj" * 3 + "KlMnO"
    asyncio.run(s.send_answer(5, "token " + token))
    assert token not in (tmp_path / "inflight.json").read_text(encoding="utf-8")
    asyncio.run(s.resend_unsent())
    assert "some secrets were hidden" in s.app.bot.sent[0]


# ---- no duplicates after a timeout
BOT_ID = 777


class RealisticBot:
    """Message ids count up, forwarding by id returns the content, a lost acknowledgement stores the message."""
    id = BOT_ID

    def __init__(self):
        self.chat, self.next, self.lose, self.forwards = {}, 100, [], 0   # lose: "ack" / "all" for the next sends

    def add(self, **kw):
        self.next += 1
        self.chat[self.next] = SimpleNamespace(**{"document": None, "text": None, "sender": BOT_ID, "message_id": self.next, **kw})
        return self.chat[self.next]

    def accept(self, **kw):
        how = self.lose.pop(0) if self.lose else None
        if how == "all":
            raise NetworkError("down")
        m = self.add(**kw)
        if how == "ack":
            raise TimedOut()
        return m

    async def send_message(self, chat, text, parse_mode=None, **kw):
        if parse_mode:
            text = text.replace("<b>", "").replace("</b>", "").replace("&lt;", "<").replace("&amp;", "&")
        return self.accept(text=text.strip())

    async def send_document(self, chat, f, filename=None, **kw):
        return self.accept(document=SimpleNamespace(file_size=len(f.read())))

    async def forward_message(self, chat, src, mid, disable_notification=None):
        assert disable_notification
        self.forwards += 1
        if mid not in self.chat:
            raise BadRequest("Message to forward not found")
        m = self.chat[mid]
        c = self.add(text=m.text, document=m.document)
        c.forward_origin = SimpleNamespace(sender_user=SimpleNamespace(id=m.sender))
        return c

    async def delete_message(self, chat, mid):
        del self.chat[mid]

    def texts(self):
        return [m.text for m in self.chat.values() if m.text is not None]


def realistic(tmp_path, bot=None, **general):
    s = make(tmp_path, **general)
    s.app = SimpleNamespace(bot=bot or RealisticBot())
    if not bot:
        asyncio.run(s.send(5, "working"))   # the status message: the last id the bridge knows
    return s


def test_answer_that_arrived_despite_a_timeout_is_not_sent_again(tmp_path):
    s = realistic(tmp_path)
    s.app.bot.lose = ["ack"]
    asyncio.run(s.send_answer(5, "a < b **bold** & more", footer="· 5% context"))
    s2 = realistic(tmp_path, s.app.bot)   # also after a restart
    asyncio.run(s2.resend_unsent())
    assert s.app.bot.texts() == ["working", "a < b bold & more\n\n· 5% context"] and s2.journal.unsent("claude") == []


def test_answer_that_did_not_arrive_is_sent_once(tmp_path):
    s = realistic(tmp_path)
    s.app.bot.lose = ["all", "ack"]   # first try lost; the retry arrives but its acknowledgement is lost
    asyncio.run(s.send_answer(5, "answer"))
    for _ in range(3):
        asyncio.run(s.resend_unsent())
    assert s.app.bot.texts() == ["working", "answer"] and s.journal.unsent("claude") == []


def test_timeout_in_the_middle_of_a_long_answer(tmp_path):
    s = realistic(tmp_path, document_threshold_chars=100_000)
    s.app.bot.lose = [None, "ack"]
    asyncio.run(s.send_answer(5, "\n".join(f"line {i}" for i in range(1500))))
    asyncio.run(s.resend_unsent())
    lines = [ln for t in s.app.bot.texts()[1:] for ln in t.split("\n") if ln]
    assert lines == [f"line {i}" for i in range(1500)]


def test_answer_file_that_arrived_is_not_sent_again(tmp_path):
    s = realistic(tmp_path, document_threshold_chars=10)
    s.app.bot.lose = [None, "ack"]
    asyncio.run(s.send_answer(5, "ž" * 50, footer="foot"))
    asyncio.run(s.resend_unsent())
    assert sum(m.document is not None for m in s.app.bot.chat.values()) == 1 and s.app.bot.texts()[-1] == "foot"


def test_same_text_from_the_user_or_before_the_answer_does_not_count(tmp_path):
    s = realistic(tmp_path)
    asyncio.run(s.send_answer(5, "ok"))   # an older identical answer
    s.app.bot.add(text="ok", sender=5)    # the user wrote the same
    s.app.bot.lose = ["all"]
    asyncio.run(s.send_answer(5, "ok"))
    asyncio.run(s.resend_unsent())
    assert s.app.bot.texts() == ["working", "ok", "ok", "ok"]


def test_lookup_stays_saved_when_the_network_is_still_down(tmp_path):
    s = realistic(tmp_path)
    s.app.bot.lose = ["ack"]
    asyncio.run(s.send_answer(5, "answer"))
    real = s.app.bot.forward_message

    async def down(*a, **kw):
        raise NetworkError("down")
    s.app.bot.forward_message = down
    asyncio.run(s.resend_unsent())
    assert len(s.journal.unsent("claude")) == 1
    s.app.bot.forward_message = real
    asyncio.run(s.resend_unsent())
    assert s.app.bot.texts() == ["working", "answer"] and s.journal.unsent("claude") == []


def test_no_lookup_when_nothing_failed(tmp_path):
    s = realistic(tmp_path)
    asyncio.run(s.send_answer(5, "answer"))
    asyncio.run(s.resend_unsent())
    assert s.app.bot.forwards == 0 and s.app.bot.texts() == ["working", "answer"]


# ---- no silent drops, edits
class Msg:
    voice = audio = photo = video = video_note = animation = document = text = caption = None
    chat_id, message_id = 5, 1

    def __init__(self, **kw):
        self.replies = []
        self.__dict__.update(kw)

    async def reply_text(self, t, **kw):
        self.replies.append(t)
        return Status()


def send(s, m, edited=False):
    u = SimpleNamespace(effective_message=m, effective_user=SimpleNamespace(id=5), effective_chat=SimpleNamespace(id=5),
                        edited_message=m if edited else None)
    asyncio.run(s.on_update(u, SimpleNamespace(bot=s.app.bot)))


def test_sticker_or_location_gets_a_reply(tmp_path):
    s, m = make(tmp_path), Msg()
    send(s, m)
    assert m.replies == [bridge.UNSUPPORTED] and s.queue.empty()


def test_big_document_is_not_called_a_video(tmp_path):
    s = make(tmp_path)

    async def dl(*a):
        raise BadRequest("File is too big")
    s.download = dl
    m = Msg(document=SimpleNamespace(file_id="f", file_name="a.zip", mime_type="application/zip"))
    send(s, m)
    assert m.replies == [bridge.TOO_BIG]


def test_edits_are_not_run(tmp_path):
    s, m = make(tmp_path), Msg(text="push it")
    send(s, m, edited=True)
    assert m.replies == [bridge.EDIT_IGNORED] and s.queue.empty()
    live_location = Msg()
    send(s, live_location, edited=True)
    assert live_location.replies == []
    send(s, m)
    assert s.queue.qsize() == 1
