"""Pinned lock-status message."""
import asyncio
from datetime import datetime
from types import SimpleNamespace

import pyotp
from telegram.error import BadRequest, NetworkError

from telegram_bridge import settings, statuspin
from telegram_bridge.auth import Auth
from telegram_bridge.bridge import BotService

SECRET = pyotp.random_base32()
T0 = datetime(2026, 11, 13, 9, 0).timestamp()


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


class Msg:
    def __init__(self, mid):
        self.message_id = mid


class Bot:
    def __init__(self):
        self.sent, self.edits, self.pins = [], [], []
        self.edit_error = None
        self.next_id = 100

    async def send_message(self, chat, text, **kw):
        self.next_id += 1
        self.sent.append((chat, text, kw))
        return Msg(self.next_id)

    async def edit_message_text(self, chat_id, message_id, text, **kw):
        if self.edit_error:
            raise self.edit_error
        self.edits.append((chat_id, message_id, text))

    async def pin_chat_message(self, chat, mid, **kw):
        self.pins.append((chat, mid, kw))

    def calls(self):
        return len(self.sent) + len(self.edits) + len(self.pins)


def auth(clock=None, idle=3.0, session=12.0):
    return Auth([5], SECRET, session_hours=session, clock=clock or Clock(), idle_lock_hours=idle)


def unlock(a):
    assert a.unlock(5, pyotp.TOTP(SECRET).at(a.clock()))[0]


def make(tmp_path, a=None, bot=None, **general):
    cfg = settings.load_config()
    s = BotService("claude", cfg["bots"]["claude"], "t", a or auth(), {**cfg["general"], **general},
                   pin_path=tmp_path / "status.json")
    s.app = SimpleNamespace(bot=bot or Bot())
    s.pin_active = s.pin_enabled
    return s


def run(coro):
    return asyncio.run(coro)


def test_text_locked_unlocked_idle_and_other_day():
    a = auth()
    assert statuspin.status_text(a, 5) == "🔒 Locked · send /unlock <code>"
    unlock(a)
    assert statuspin.status_text(a, 5) == "🔓 Unlocked until 21:00 (idle lock after 3 h without messages)"
    a.idle_s = 1.5 * 3600
    assert statuspin.status_text(a, 5).endswith("(idle lock after 1.5 h without messages)")
    a.idle_s = 0
    assert statuspin.status_text(a, 5) == "🔓 Unlocked until 21:00"
    b = auth(session=20)
    unlock(b)
    assert statuspin.status_text(b, 5).startswith("🔓 Unlocked until 14. 11. 05:00")


def test_first_run_sends_and_pins_once_then_idle(tmp_path):
    s = make(tmp_path)
    run(s.update_pin(5))
    run(s.update_pin(5))
    bot = s.app.bot
    assert bot.sent == [(5, "🔒 Locked · send /unlock <code>", {"disable_notification": True})]
    assert bot.pins == [(5, 101, {"disable_notification": True})] and not bot.edits


def test_state_change_edits(tmp_path):
    a = auth()
    s = make(tmp_path, a)
    run(s.update_pin(5))
    unlock(a)
    run(s.update_pin(5))
    assert s.app.bot.edits == [(5, 101, "🔓 Unlocked until 21:00 (idle lock after 3 h without messages)")]
    assert len(s.app.bot.sent) == 1


def test_expiry_and_idle_lock_detected_by_tick(tmp_path):
    a = auth()
    s = make(tmp_path, a)
    unlock(a)
    run(s.update_pin(5))
    a.clock.t += 3 * 3600 + 1
    run(s.update_pin(5))
    assert s.app.bot.edits[-1][2] == statuspin.LOCKED_TEXT


def test_edit_not_found_resends_and_pins(tmp_path):
    a = auth()
    s = make(tmp_path, a)
    run(s.update_pin(5))
    unlock(a)
    s.app.bot.edit_error = BadRequest("Message to edit not found")
    run(s.update_pin(5))
    bot = s.app.bot
    assert len(bot.sent) == 2 and len(bot.pins) == 2 and bot.pins[1][1] == 102
    assert s.pin_store.get("claude", 5) == 102


def test_not_modified_ignored_and_other_errors_retry(tmp_path):
    a = auth()
    s = make(tmp_path, a)
    run(s.update_pin(5))
    unlock(a)
    s.app.bot.edit_error = BadRequest("Message is not modified")
    run(s.update_pin(5))
    assert len(s.app.bot.sent) == 1
    run(s.update_pin(5))      # remembered as done: no further call
    a.clock.t += 60
    s.app.bot.edit_error = NetworkError("down")
    a.lock(5)
    run(s.update_pin(5))      # error: nothing crashes, nothing stored
    s.app.bot.edit_error = None
    run(s.update_pin(5))
    assert s.app.bot.edits[-1][2] == statuspin.LOCKED_TEXT


def test_disabled_makes_no_calls(tmp_path):
    s = make(tmp_path, status_pin=False)
    run(s.update_pin(5))
    assert s.app.bot.calls() == 0 and not s.pin_enabled


def test_persists_across_instances(tmp_path):
    a = auth()
    s1 = make(tmp_path, a)
    run(s1.update_pin(5))
    s2 = make(tmp_path, a)
    unlock(a)
    run(s2.update_pin(5))
    assert not s2.app.bot.sent and s2.app.bot.edits[0][1] == 101


def test_unlock_and_lock_commands_update_immediately(tmp_path):
    a = auth()
    s = make(tmp_path, a)
    run(s.update_pin(5))
    replies = []

    class M:
        text, caption, chat_id, message_id = "/unlock x", None, 5, 1
        voice = audio = photo = document = None

        async def delete(self):
            pass

        async def reply_text(self, t, **kw):
            replies.append(t)

    async def go(text):
        M.text = text
        upd = SimpleNamespace(effective_message=M(), effective_user=SimpleNamespace(id=5))
        await s.on_update(upd, SimpleNamespace(bot=s.app.bot))
    code = pyotp.TOTP(SECRET).at(a.clock())
    run(go(f"/unlock {code}"))
    assert s.app.bot.edits[-1][2].startswith("🔓 Unlocked until")
    run(go("/lock"))
    assert s.app.bot.edits[-1][2] == statuspin.LOCKED_TEXT
