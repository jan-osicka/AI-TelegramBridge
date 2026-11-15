"""In-flight journal (inflight.json): the running task and the waiting queue per bot and chat, so a restart can offer /resume.
Layout: {"bots": {bot: {chat: {"running": rec|None, "queue": [rec], "pending": {"task": rec|None, "queue": [rec]}}}}}.
"running"/"queue" belong to this process; at startup they are moved to "pending" (the interrupted work awaiting /resume or /drop).
Written atomically; a corrupt file is treated as empty. With path=None everything stays in memory."""
import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("bridge.journal")

MAX_AGE_DAYS = 7


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def age_days(rec, clock=time.time):
    try:
        return (clock() - datetime.fromisoformat(rec["started"]).timestamp()) / 86400
    except (KeyError, TypeError, ValueError):
        return float("inf")   # missing/garbled timestamp: treat as expired


class Journal:
    def __init__(self, path=None, clock=time.time):
        self.path = Path(path) if path else None
        self.clock = clock
        self._lock = threading.RLock()
        self._mem = {"bots": {}}

    def _read(self):
        if self.path is None:
            return self._mem
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(d, dict) or not isinstance(d.get("bots"), dict):
                raise ValueError("unexpected structure")
            return d
        except FileNotFoundError:
            return {"bots": {}}
        except Exception as e:
            log.warning("inflight file %s is corrupt (%s); treating as empty", self.path, type(e).__name__)
            return {"bots": {}}

    def _write(self, d):
        if self.path is None:
            self._mem = d
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(json.dumps(d, ensure_ascii=False))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except OSError:
            log.exception("could not save %s", self.path)

    @staticmethod
    def _entry(d, bot, chat):
        e = d["bots"].setdefault(bot, {}).setdefault(str(chat), {})
        e.setdefault("running", None)
        e.setdefault("queue", [])
        return e

    @staticmethod
    def _prune(d):
        for bot in list(d["bots"]):
            for chat in list(d["bots"][bot]):
                e = d["bots"][bot][chat]
                if not e.get("running") and not e.get("queue") and not e.get("pending"):
                    del d["bots"][bot][chat]
            if not d["bots"][bot]:
                del d["bots"][bot]

    def _update(self, fn):
        with self._lock:
            d = self._read()
            out = fn(d)
            self._prune(d)
            self._write(d)
            return out

    # ---- live bookkeeping
    def queue_add(self, bot, chat, rec):
        self._update(lambda d: self._entry(d, bot, chat)["queue"].append(rec))

    def queue_clear(self, bot, chat):
        self._update(lambda d: self._entry(d, bot, chat).update(queue=[]))

    def start(self, bot, chat, rec):
        """The task leaves the queue and becomes the running one."""
        def fn(d):
            e = self._entry(d, bot, chat)
            e["queue"] = [r for r in e["queue"] if r.get("jid") != rec.get("jid")]
            e["running"] = rec
        self._update(fn)

    def finish(self, bot, chat, jid):
        def fn(d):
            e = self._entry(d, bot, chat)
            if e["running"] and e["running"].get("jid") == jid:
                e["running"] = None
        self._update(fn)

    def discard(self, bot, chat, jid):
        """A queued item that was dropped without running."""
        def fn(d):
            e = self._entry(d, bot, chat)
            e["queue"] = [r for r in e["queue"] if r.get("jid") != jid]
        self._update(fn)

    # ---- restart handling
    def recover(self, bot):
        """At startup: move leftover running/queue to pending, drop anything older than 7 days.
        Returns {chat_int: {"task": rec|None, "queue": [rec]}} of everything awaiting /resume."""
        def fn(d):
            for chat, e in list(d["bots"].get(bot, {}).items()):
                old = e.get("pending") or {}
                recs = [old.get("task")] + list(old.get("queue") or []) + [e.get("running")] + list(e.get("queue") or [])
                e["running"], e["queue"] = None, []
                recs = [r for r in recs if r and age_days(r, self.clock) <= MAX_AGE_DAYS]
                e["pending"] = {"task": recs[0], "queue": recs[1:]} if recs else None
                if not e.get("pending"):
                    e.pop("pending", None)
            out = {}
            for chat, e in d["bots"].get(bot, {}).items():
                if e.get("pending"):
                    try:
                        out[int(chat)] = e["pending"]
                    except ValueError:
                        pass
            return out
        return self._update(fn)

    def pending(self, bot, chat):
        with self._lock:
            return (self._read()["bots"].get(bot, {}).get(str(chat)) or {}).get("pending")

    def take_pending(self, bot, chat):
        def fn(d):
            e = d["bots"].get(bot, {}).get(str(chat))
            return e.pop("pending", None) if e else None
        return self._update(fn)
