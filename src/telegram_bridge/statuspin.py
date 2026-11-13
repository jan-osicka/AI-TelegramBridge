"""Pinned lock-status message: text building and the stored message ids (status.json)."""
import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path

log = logging.getLogger("bridge.statuspin")

LOCKED_TEXT = "🔒 Locked · send /unlock <code>"
INTERVAL_S = 30


def _hours(h):
    return f"{h:g}"


def status_text(auth, uid):
    """Current pin text for one user. Has the same side effects as auth.is_unlocked (lazy expiry)."""
    exp = auth.expiry(uid)
    if exp is None:
        return LOCKED_TEXT
    now = auth.clock()
    when = datetime.fromtimestamp(exp)
    if when.date() == datetime.fromtimestamp(now).date():
        stamp = f"{when:%H:%M}"
    else:
        stamp = f"{when.day}. {when.month}. {when:%H:%M}"
    text = f"🔓 Unlocked until {stamp}"
    if auth.idle_s:
        text += f" (idle lock after {_hours(auth.idle_s / 3600)} h without messages)"
    return text


class PinStore:
    """{"bots": {bot: {user_id: message_id}}}, written atomically."""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _read(self):
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(d, dict) and isinstance(d.get("bots"), dict):
                return d
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning("status file unreadable (%s); starting empty", type(e).__name__)
        return {"bots": {}}

    def get(self, bot, uid):
        try:
            return int((self._read()["bots"].get(bot) or {}).get(str(uid)))
        except (TypeError, ValueError):
            return None

    def set(self, bot, uid, message_id):
        with self._lock:
            d = self._read()
            d["bots"].setdefault(bot, {})[str(uid)] = int(message_id)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_name(self.path.name + ".tmp")
                tmp.write_text(json.dumps(d), encoding="utf-8")
                os.replace(tmp, self.path)
            except OSError:
                log.exception("could not save %s", self.path)
