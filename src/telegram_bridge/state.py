"""Persistent per-bot state (sessions, cwds, chat-mode history, context info) in one JSON file, keyed by bot name.
Never stores images (list contents are reduced to their text plus "[image]")."""
import json
import logging
import os
import threading
from pathlib import Path

log = logging.getLogger("bridge.state")


def _plain(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text = " ".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
        return (text + " [image]").strip()
    return str(content)


def clean_history(history):
    """{chat_id: [messages]} with every content reduced to a string (no base64 images)."""
    return {str(c): [{"role": m.get("role", "user"), "content": _plain(m.get("content", ""))} for m in msgs]
            for c, msgs in history.items() if msgs}


def _clean_ctx(v):
    def num(x):
        return int(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None
    out = {"tokens": num(v.get("tokens")), "window": num(v.get("window")), "pct": num(v.get("pct")),
           "warned": sorted({int(x) for x in (v.get("warned") or []) if isinstance(x, (int, float))}),
           "trimmed": bool(v.get("trimmed"))}
    if num(v.get("last")) is not None:
        out["last"] = float(v["last"])   # wall-clock time of the last agent reply (idle reset)
    return out


class StateStore:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _read(self):
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(d, dict) or not isinstance(d.get("bots", {}), dict):
                raise ValueError("unexpected structure")
            return d
        except FileNotFoundError:
            return {"bots": {}}
        except Exception as e:
            log.warning("sessions file %s is corrupt (%s); starting empty", self.path, type(e).__name__)
            return {"bots": {}}

    def load(self, bot):
        """Returns {"sessions", "cwds", "chats", "ctx", "parked"}: {int: str}, {int: str}, {int: [msgs]},
        {int: {"tokens", "window", "pct", "warned": [levels], "trimmed": bool}}, {int: {"sid", "ctx"}} (empty parts if none)."""
        b = self._read().get("bots", {}).get(bot) or {}
        out = {"sessions": {}, "cwds": {}, "chats": {}, "ctx": {}, "parked": {}}
        try:
            out["sessions"] = {int(k): str(v) for k, v in (b.get("sessions") or {}).items()}
            out["cwds"] = {int(k): str(v) for k, v in (b.get("cwds") or {}).items()}
            out["chats"] = {int(k): clean_history({k: v})[str(k)] for k, v in (b.get("chats") or {}).items() if v}
            out["ctx"] = {int(k): _clean_ctx(v) for k, v in (b.get("ctx") or {}).items() if isinstance(v, dict)}
            out["parked"] = {int(k): {"sid": str(v["sid"]), "ctx": _clean_ctx(v.get("ctx") or {})}
                             for k, v in (b.get("parked") or {}).items() if isinstance(v, dict) and v.get("sid")}
        except Exception as e:
            log.warning("sessions for bot %s unreadable (%s); starting empty", bot, type(e).__name__)
            return {"sessions": {}, "cwds": {}, "chats": {}, "ctx": {}, "parked": {}}
        return out

    def save(self, bot, sessions, cwds, chats, ctx=None, parked=None):
        with self._lock:
            d = self._read()
            d.setdefault("bots", {})[bot] = {
                "sessions": {str(k): v for k, v in sessions.items()},
                "cwds": {str(k): v for k, v in cwds.items()},
                "chats": clean_history(chats),
                "ctx": {str(k): _clean_ctx(v) for k, v in (ctx or {}).items() if v},
                "parked": {str(k): {"sid": v["sid"], "ctx": _clean_ctx(v["ctx"])} for k, v in (parked or {}).items()},
            }
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_name(self.path.name + ".tmp")
                tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, self.path)
            except OSError:
                log.exception("could not save %s", self.path)
