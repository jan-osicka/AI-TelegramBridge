"""Telegram -> Claude Code / local model bridge. Two bots, one process, long polling."""
import asyncio
import io
import logging
import logging.handlers
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

from telegram import BotCommand, ReplyParameters, Update
from telegram.error import BadRequest, Conflict
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from . import chat as chatmod
from . import ctxinfo
from . import fmt
from . import housekeeping
from . import journal as journalmod
from . import media
from . import outfiles
from . import progress
from . import redaction, statuspin
from . import settings
from . import video as videomod
from . import voice as voicemod
from .auth import Auth
from .localmodel import LocalModelError, LocalModelManager
from .runner import Runner
from .state import StateStore
from .textutil import split_message

log = logging.getLogger("bridge")

COMMANDS = [
    ("unlock", "Unlock with a 2FA code: /unlock 123456"),
    ("lock", "End the unlocked session"),
    ("exit", "Clear the context (new session)"),
    ("new", "Same as /exit"),
    ("back", "Return to the session an idle reset replaced"),
    ("stop", "Kill the running task (/stop all: also clear the queue)"),
    ("resume", "Continue the task interrupted by a restart"),
    ("drop", "Forget the task interrupted by a restart"),
    ("status", "Lock state, running task, session"),
    ("cwd", "Show or set working directory"),
    ("help", "List commands"),
]
VOICE_COMMANDS = [
    ("voice", "Voice replies: /voice on, /voice off"),
]
LOCAL_COMMANDS = [
    ("modelstart", "Start (preload) the local model server"),
    ("modelstop", "Stop the local model server now"),
]


LOCKED_NOTICE = "🔒 Locked. Send /unlock <code from authenticator>."
LOCKED_NOTICE_INTERVAL_S = 60.0
NOTE = "\n\n(full answer attached)"
REDACTED_NOTE = "\n\n(some secrets were hidden as [REDACTED])"
TYPING_INTERVAL_S = 4.0           # chat mode: resend the "typing" action this often while waiting
QUOTE = {"do_quote": True, "allow_sending_without_reply": True}   # reply_text kwargs: thread to the original


def commands_for(cfg):
    return COMMANDS + (VOICE_COMMANDS if cfg.get("voice") else []) + (LOCAL_COMMANDS if cfg.get("needs_local_model") else [])


class Redact(logging.Filter):
    def __init__(self, secrets):
        super().__init__()
        self.secrets = [s for s in secrets if s]

    def clean(self, msg):
        for s in self.secrets:
            msg = msg.replace(s, "***")
        msg = re.sub(r"bot\d+:[A-Za-z0-9_-]{20,}", "bot***", msg)
        return redaction.redact(msg)[0]

    def filter(self, record):
        record.msg, record.args = self.clean(record.getMessage()), None
        if record.exc_info:   # the formatter appends the traceback after filters run: render and mask it here
            record.exc_text = self.clean(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = self.clean(record.exc_text)
        if record.stack_info:
            record.stack_info = self.clean(record.stack_info)
        return True


def setup_logging(secrets):
    settings.LOGS.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(settings.LOGS / "bridge.log", maxBytes=1_000_000,
                                              backupCount=5, encoding="utf-8")
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in (fh, sh):
        h.addFilter(Redact(secrets))
        root.addHandler(h)
    for noisy in ("httpx", "httpcore", "telegram", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def make_local_manager(cfg):
    if not any(b.get("needs_local_model") for b in cfg["bots"].values()):
        return None
    m = cfg["local_model"]
    if not m.get("url"):
        raise ValueError("a bot needs the local model but [local_model] url is not set in config.toml")
    return LocalModelManager(m["url"], m["start_command"], m["stop_command"],
                             startup_timeout_s=float(m["startup_timeout_minutes"]) * 60,
                             idle_stop_minutes=float(m["idle_stop_minutes"]),
                             log_path=settings.LOGS / "local-model-start.log",
                             busy_file=m.get("busy_file"))


def fmt_elapsed(sec):
    sec = int(sec)
    return f"{sec // 60}m {sec % 60:02d}s" if sec >= 60 else f"{sec}s"


def reply_kw(msg_id):
    """send_message kwargs that thread a message to the user's original; a deleted original does not fail."""
    if not msg_id:
        return {}
    return {"reply_parameters": ReplyParameters(msg_id, allow_sending_without_reply=True)}


def fmt_tool(ev):
    d = ev["detail"].replace("\n", " ")
    return f"🔧 {ev['name']}: {d}" if d else f"🔧 {ev['name']}"


RESUME_PREFIX = ("The previous run of the request below was interrupted by a PC or bridge restart. Before continuing, "
                 "check what was already done (files, git status, logs) and do not repeat steps that already happened, "
                 "especially ones that cannot be undone (pushes, sent messages, deletions). Then finish the request.\n\n"
                 "Original request:\n")
MAX_RECORD_CHARS = 20000
RESTART_NOTICE = "⚠️ Bridge restarting. The task will be offered for /resume when it is back."


MAX_DOWNLOAD = 20 * 1024 * 1024
MAX_CHAT_IMAGES = 2


class BotService:
    def __init__(self, name, cfg, token, auth, general, qwen=None, clock=time.monotonic, store=None, local_url=None, pin_path=None, journal=None):
        self.clock = clock
        self.wall = time.time   # wall clock for idle times that must survive restarts
        self._notice_at = {}  # chat_id -> time of last "locked" notice
        self.name, self.cfg, self.token, self.auth, self.g = name, cfg, token, auth, general
        if self.outgoing_files:   # tell the agent how to send files, once (the config dict is not touched)
            extra = (cfg.get("append_system_prompt") or "").strip()
            sentence = outfiles.convention_sentence(settings.OUTBOX)
            self.cfg = cfg = {**cfg, "append_system_prompt": (extra + " " + sentence).strip()}
        self.voice_cfg = cfg.get("voice")   # None = voice replies are off for this bot
        self.voice_on = set()   # chat ids with voice mode on; in memory only, cleared when the session ends
        if self.voice_cfg:
            extra = (cfg.get("append_system_prompt") or "").strip()
            sentence = voicemod.convention_sentence(self.voice_cfg["voices"])
            self.cfg = cfg = {**cfg, "append_system_prompt": (extra + " " + sentence).strip()}
            auth.lock_hooks.append(lambda uid: self.voice_on.clear())   # private chats: one user per bot
        self.display = cfg.get("display_name") or name.capitalize()
        self.local_url = local_url or (qwen.url if qwen else None)
        self.qwen = qwen if cfg.get("needs_local_model") else None
        self.store = store   # StateStore or None (no persistence)
        timeout_min = float(general.get("task_timeout_minutes", 60) or 0)
        self.runner = Runner(cfg, general["cwd"], timeout_s=timeout_min * 60 or None)
        self.sessions = {}   # chat_id -> session_id
        self.mode = (cfg.get("mode") or "agent") if cfg.get("needs_local_model") else "agent"
        self.chat_enabled = self.mode in ("chat", "auto")
        self.agent_triggers = chatmod.compile_triggers(cfg.get("agent_triggers"))   # None = built-in word list
        self.save_triggers = chatmod.compile_triggers(cfg.get("save_triggers"))
        self.chats = chatmod.ChatSessions()   # chat mode history per chat_id (cleared by /exit)
        self.last_mode = {}  # chat_id -> "agent" | "chat": who handled the previous turn (auto-mode follow-ups)
        self.unseen = {}     # chat_id -> [(role, text)] chat-mode turns the agent session has not seen yet
        self.cwds = {}       # chat_id -> cwd
        self.ctx = {}        # chat_id -> {tokens, window, pct, warned: [levels], trimmed, last}
        self.parked = {}     # chat_id -> {"sid", "ctx"}: the session an idle reset replaced, for /back
        self.cold_start = False   # Strata was started for the task that is about to run
        self.heartbeat_s = float(general.get("heartbeat_seconds", 15))
        self.queue = asyncio.Queue()
        self.journal = journal or journalmod.Journal()   # in memory only unless main() passes the file-backed one
        self._offered = False
        self.shutdown_killed = False   # stop() killed a running task: that task was interrupted
        self.shutting_down = False   # a stop for restart keeps the journal: the task counts as interrupted
        self.interrupted = {}        # chat -> {"task", "queue"} left over from the previous run, awaiting /resume or /drop
        self._load_state()
        self.current = None  # (chat_id, prompt preview)
        self.app = None
        self.worker = None
        self.pin_enabled = bool(general.get("status_pin", True))
        self.pin_active = False   # set in start(): lock-state updates only touch Telegram once running
        self.pin_store = statuspin.PinStore(pin_path or settings.STATE_DIR / "status.json")
        self._pin_text = {}       # uid -> text last confirmed on Telegram
        self._pin_lock = asyncio.Lock()
        self.pin_task = None

    # ---- persistence
    def cwd_roots(self):
        return self.g.get("allowed_cwd_roots") or [self.g["cwd"]]

    def _load_state(self):
        if not self.store:
            return
        d = self.store.load(self.name)
        self.sessions = d["sessions"]
        self.cwds = {c: p for c, p in d["cwds"].items()
                     if Path(p).is_dir() and housekeeping.cwd_allowed(p, self.cwd_roots())}
        self.chats.history = d["chats"]
        self.ctx = d["ctx"]
        self.parked = d["parked"]

    def save_state(self):
        if self.store:
            self.store.save(self.name, self.sessions, self.cwds, self.chats.history, self.ctx, self.parked)

    def ctx_entry(self, chat):
        return self.ctx.setdefault(chat, {"tokens": None, "window": None, "pct": None, "warned": [], "trimmed": False})

    def reset_session(self, chat):
        """/exit, /new, /cwd: forget the agent session, chat history and everything tracked about the context."""
        self.sessions.pop(chat, None)
        self.last_mode.pop(chat, None)
        self.unseen.pop(chat, None)
        self.chats.clear(chat)
        self.ctx.pop(chat, None)
        self.parked.pop(chat, None)
        self.save_state()

    def idle_reset_due(self, chat):
        """Seconds idle if the session should be replaced (idle past the prompt-cache lifetime and big), else None."""
        mins = float(self.cfg.get("idle_new_session_minutes") or 0)
        e = self.ctx.get(chat) or {}
        last, tokens = e.get("last"), e.get("tokens") or 0
        if not mins or last is None or tokens < int(self.cfg.get("idle_new_session_tokens", 30000)):
            return None
        idle = self.wall() - last
        return idle if idle >= mins * 60 else None

    # ---- helpers
    def help(self):
        return "\n".join(f"/{c} - {d}" for c, d in commands_for(self.cfg))

    async def qwen_start(self, chat):
        """Ensure Qwen is serving; notify the chat if a startup is needed. Returns True if ready."""
        bot = self.app.bot
        if not (self.qwen.starting or not await self.qwen.is_up()):
            return True
        await bot.send_message(chat, f"⏳ Starting {self.display}, ~2 min…")
        try:
            await self.qwen.ensure()
        except LocalModelError as e:
            await self.send(chat, f"⚠️ {e}")
            return False
        self.cold_start = True
        await bot.send_message(chat, f"✅ {self.display} is ready.")
        return True

    @property
    def outgoing_files(self):
        return bool(self.g.get("outgoing_files", True))

    def allowed_dirs(self):
        return outfiles.allowed_dirs(self.g)

    def cwd_for(self, chat_id):
        return self.cwds.get(chat_id, self.g["cwd"])

    async def reply(self, update, text):
        for chunk in split_message(text):
            await self.say(update.effective_message, chunk)

    # ---- sending
    async def say(self, msg, text, **kw):
        """reply_text with secrets masked; use for every non-constant reply."""
        text, _ = redaction.redact(text)
        return await msg.reply_text(text, **kw)

    async def send(self, chat, text, reply_to=None):
        text, _ = redaction.redact(text)
        return await self.app.bot.send_message(chat, text, **reply_kw(reply_to))

    async def send_html(self, chat, html, plain, reply_to=None):
        """Send as Telegram HTML; if Telegram rejects it, resend the same chunk as plain text."""
        bot = self.app.bot
        try:
            return await bot.send_message(chat, html, parse_mode="HTML", **reply_kw(reply_to))
        except BadRequest as e:
            log.warning("[%s] HTML rejected (%s); resending as plain text", self.name, e)
            return await bot.send_message(chat, plain, **reply_kw(reply_to))

    async def send_rich(self, chat, text, reply_to=None, footer=""):
        """Markdown -> Telegram HTML chunks (tag-safe split), the first one threaded to the user's message."""
        text, _ = redaction.redact(text)   # raw text, before Markdown -> HTML and the split
        chunks = fmt.with_footer(fmt.render_chunks(text), footer)
        for i, (html, plain) in enumerate(chunks):
            await self.send_html(chat, html, plain, reply_to if i == 0 else None)

    async def send_answer(self, chat, text, reply_to=None, footer=""):
        """A final answer: inline if short, else a 500-char preview plus answer.md. The footer follows either way."""
        text, hidden = redaction.redact(text)
        long = len(text) > int(self.g["document_threshold_chars"])
        if hidden and not long:
            text += REDACTED_NOTE
        if long:
            html, plain = fmt.preview_html(text, 500)
            extra = NOTE + (REDACTED_NOTE if hidden else "")
            await self.send_html(chat, html + extra, plain + extra, reply_to)
            await self.app.bot.send_document(chat, io.BytesIO(text.encode("utf-8")), filename="answer.md")
            if footer:
                await self.send(chat, footer)
        else:
            await self.send_rich(chat, text, reply_to, footer)

    # ---- update handling
    async def on_update(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        msg, user = update.effective_message, update.effective_user
        if msg is None or user is None:
            return
        uid = user.id
        text = msg.text or msg.caption or ""
        cmd, arg = None, ""
        if msg.text and msg.text.startswith("/"):
            head, _, arg = msg.text.partition(" ")
            cmd = head[1:].split("@")[0].lower()
            arg = arg.strip()

        if not self.auth.is_allowed(uid):
            if cmd == "whoami" and self.auth.whoami_enabled():
                await self.say(msg, f"Your Telegram user ID: {uid}")
                log.info("[%s] /whoami answered for uid=%s", self.name, uid)
            else:
                log.warning("[%s] ignored update from non-whitelisted uid=%s", self.name, uid)
            return

        if cmd == "unlock":
            ok, info = self.auth.unlock(uid, arg)  # never log the code
            log.info("[%s] unlock by %s: %s", self.name, uid, "ok" if ok else "fail")
            try:
                await msg.delete()
            except Exception:
                pass
            await ctx.bot.send_message(msg.chat_id, ("🔓 " if ok else "🔒 ") + info +
                                       (f" Valid until {datetime.fromtimestamp(self.auth.expiry(uid)):%H:%M %d.%m.}" if ok else ""))
            if ok:
                self.voice_on.clear()   # a new session starts with voice mode off
                await self.update_pin(uid)
            return
        if not self.auth.is_unlocked(uid):
            log.info("[%s] ignored (locked) from uid=%s", self.name, uid)
            now = self.clock()
            last = self._notice_at.get(msg.chat_id)
            if last is None or now - last >= LOCKED_NOTICE_INTERVAL_S:
                self._notice_at[msg.chat_id] = now
                try:
                    await msg.reply_text(LOCKED_NOTICE)
                except Exception:
                    log.exception("[%s] could not send locked notice", self.name)
            return

        self.auth.touch(uid)   # idle auto-lock counts any accepted message or command
        chat = msg.chat_id
        if cmd:
            await self.command(update, cmd, arg, uid, chat)
            return

        item = await self.build_prompt(update, ctx, text)
        if item is None:
            return
        busy = self.runner.running or not self.queue.empty()
        item["msg_id"] = getattr(msg, "message_id", None)
        await self.enqueue(chat, uid, item)
        if busy:
            await msg.reply_text(f"⏳ Queued (position {self.queue.qsize()}). One task at a time.", **QUOTE)

    # ---- in-flight journal
    def record(self, chat, uid, item):
        """Journal entry of one task: plain JSON, keeps the user's own text and downloaded file paths only."""
        text = item.get("text") or ""
        prompt = item.get("orig_prompt") or item.get("prompt") or ""
        prompt, text = prompt[:MAX_RECORD_CHARS], text[:MAX_RECORD_CHARS]
        return {"chat": chat, "uid": uid, "mode": item.get("mode", "agent"), "prompt": prompt,
                "text": text, "user_text": (text or prompt)[:200], "images": [str(p) for p in item.get("images") or []],
                "session_id": item.get("session_id") or self.sessions.get(chat), "started": journalmod.now_iso(), "msg_id": item.get("msg_id"),
                "jid": item.get("jid"), "save": bool(item.get("save")), "resume_count": int(item.get("resume_count", 0))}

    async def enqueue(self, chat, uid, item):
        item.setdefault("jid", uuid.uuid4().hex)
        item.setdefault("uid", uid)
        if not item.get("save"):   # memory-save follow-ups are not worth resuming
            self.journal.queue_add(self.name, chat, self.record(chat, uid, item))
        await self.queue.put((chat, uid, item))

    def item_from(self, rec, resumed=False):
        item = {"mode": rec.get("mode", "agent"), "prompt": rec.get("prompt", ""), "text": rec.get("text", ""),
                "images": [Path(p) for p in rec.get("images") or []], "msg_id": rec.get("msg_id"),
                "uid": rec.get("uid"), "save": bool(rec.get("save")), "resume_count": int(rec.get("resume_count", 0))}
        if resumed:
            item["resume_count"] += 1
            if rec.get("session_id") and item["mode"] != "chat":
                item["session_id"] = rec["session_id"]   # the run continues the recorded session, even after /new
            if item["mode"] != "chat":
                item["orig_prompt"] = item["prompt"]
                item["prompt"] = RESUME_PREFIX + item["prompt"]
        return item

    async def offer_resume(self):
        """After a start: tell the user's private chat what the restart interrupted (needs no unlock to send)."""
        if self._offered:
            return
        self._offered = True
        self.interrupted = self.journal.recover(self.name)
        for chat, p in sorted(self.interrupted.items()):
            if not self.auth.is_allowed(chat):
                continue
            task, queue = p.get("task"), p.get("queue") or []
            lines = []
            if task:
                try:
                    hhmm = f"{datetime.fromisoformat(task['started']):%H:%M}"
                except (KeyError, ValueError):
                    hhmm = "?"
                twice = " (interrupted twice)" if int(task.get("resume_count", 0)) >= 1 else ""
                lines.append(f"⚠️ Restarted while I was working on: “{task.get('user_text', '')}” (started {hhmm}){twice}. "
                             "Send /resume to continue or /drop to forget it.")
            else:
                lines.append("⚠️ Restarted with queued messages. Send /resume to run them or /drop to forget them.")
            if queue:
                lines.append(f"Also waiting: {len(queue)} queued message(s).")
                lines += [f"- {(r.get('user_text') or '')[:60]}" for r in queue]
            try:
                await self.send(chat, "\n".join(lines))
            except Exception as e:
                log.warning("[%s] resume offer to %s failed: %s", self.name, chat, type(e).__name__)

    async def command(self, update, cmd, arg, uid, chat):
        msg = update.effective_message
        if cmd in ("help", "start"):
            await msg.reply_text(self.help())
        elif cmd == "voice":
            await msg.reply_text(self.voice_command(chat, arg))
        elif cmd == "lock":
            self.auth.lock(uid)
            await msg.reply_text("🔒 Locked.")
            await self.update_pin(uid)
        elif cmd in ("exit", "new"):
            self.reset_session(chat)
            await msg.reply_text("Context cleared.")
        elif cmd == "back":
            if self.runner.running:
                await msg.reply_text("Busy: wait until the current task finishes, then send /back again.")
                return
            p = self.parked.pop(chat, None)
            if not p:
                await msg.reply_text("Nothing to go back to.")
                return
            self.sessions[chat], e = p["sid"], p["ctx"]
            e["last"] = self.wall()   # do not trip the idle reset again on the next message
            self.ctx[chat] = e
            self.save_state()
            await msg.reply_text(ctxinfo.back_notice(e.get("tokens")))
        elif cmd in ("modelstart", "qwenstart") and self.qwen:
            await self.qwen_start(chat)
        elif cmd in ("modelstop", "qwenstop") and self.qwen:
            if self.runner.running or self.qwen.busy:
                await msg.reply_text(f"A {self.display} task is running; /stop it first.")
                return
            try:
                await self.qwen.stop()
                await msg.reply_text(f"{self.display} stopped." if not await self.qwen.is_up()
                                     else f"Stop command ran but {self.display} still answers.")
            except LocalModelError as e:
                await self.say(msg, f"⚠️ {e}")
        elif cmd == "resume":
            if self.runner.running or self.current or not self.queue.empty():
                await msg.reply_text("Busy: wait until the current task finishes, then send /resume again.")
                return
            p = self.journal.take_pending(self.name, chat)
            self.interrupted.pop(chat, None)
            if not p:
                await msg.reply_text("Nothing to resume.")
                return
            task, queue = p.get("task"), p.get("queue") or []
            if task:
                await self.enqueue(chat, task.get("uid") or uid, self.item_from(task, resumed=True))
            for r in queue:
                await self.enqueue(chat, r.get("uid") or uid, self.item_from(r))
            await msg.reply_text("Resuming." + (f" {len(queue)} queued message(s) follow." if queue and task else ""))
        elif cmd == "drop":
            self.journal.take_pending(self.name, chat)
            self.interrupted.pop(chat, None)
            await msg.reply_text("Dropped.")
        elif cmd == "stop":
            clear_all = arg.lower() == "all"
            dropped = 0
            if clear_all:
                while not self.queue.empty():
                    c, _u, it = self.queue.get_nowait()
                    self.journal.discard(self.name, c, it.get("jid"))
                    dropped += 1
            killed = self.runner.stop()
            n = self.queue.qsize()
            if clear_all:
                text = ("Killing the running task. " if killed else "Nothing is running. ") + f"Cleared {dropped} queued task(s)."
            elif killed:
                text = "Killing the running task." + (f" {n} queued task(s) will continue; /stop all clears them too." if n else
                                                       " (/stop all also clears the queue.)")
            else:
                text = "Nothing is running." + (f" {n} queued; /stop all clears them." if n else "")
            await self.say(msg, text)
        elif cmd == "status":
            exp = self.auth.expiry(uid)
            lines = [f"Bot: {self.display}",
                     f"Lock: unlocked until {datetime.fromtimestamp(exp):%H:%M %d.%m.}" if exp else "Lock: locked",
                     f"Running: {self.current[1] if self.runner.running and self.current else 'no'}",
                     f"Queued: {self.queue.qsize()}",
                     f"Session: {self.sessions.get(chat, '(none)')}",
                     *self.context_status(chat),
                     *([f"Mode: {self.mode} (chat history: {len(self.chats.history.get(chat, []))} msgs)"] if self.qwen else []),
                     f"cwd: {self.cwd_for(chat)}"]
            if self.qwen:
                state = "starting" if self.qwen.starting else ("running" if await self.qwen.is_up() else "stopped")
                lines.append(f"{self.display}: {state}")
            await self.say(msg, "\n".join(lines))
        elif cmd == "cwd":
            if not arg:
                await self.say(msg, str(self.cwd_for(chat)))
            elif Path(arg).is_dir():
                if not housekeeping.cwd_allowed(arg, self.cwd_roots()):
                    log.warning("[%s] /cwd refused: %s", self.name, arg)
                    await msg.reply_text("Not allowed.")
                    return
                self.cwds[chat] = str(Path(arg).resolve())
                self.reset_session(chat)
                await self.say(msg, f"cwd set to {self.cwds[chat]} (session reset)")
            else:
                await msg.reply_text("Not a directory.")
        else:
            await msg.reply_text("Unknown command.\n" + self.help())

    def voice_command(self, chat, arg):
        if not self.voice_cfg:
            return "Voice replies are not configured for this bot (see [bots.<name>.voice] in config.toml)."
        arg = arg.strip().lower()
        if arg == "on":
            self.voice_on.add(chat)
        elif arg == "off":
            self.voice_on.discard(chat)
        elif arg:
            return "Usage: /voice on, /voice off"
        return ("🔊 Voice mode is on: replies come as a voice message plus text." if chat in self.voice_on
                else "🔇 Voice mode is off (text only). It also turns off when the session locks.")

    def voice_split(self, chat, text):
        """(text without the [tg-voice] block, (lang, spoken text) or None). In voice mode a reply without a
        block is spoken in a cleaned version of its text."""
        v = self.voice_cfg
        if not v or not text:
            return text, None
        text, spoken = voicemod.extract(text, v["voices"], v["default_lang"])
        if spoken is None and chat in self.voice_on and (plain := voicemod.clean_text(text)):
            spoken = (v["default_lang"], plain)
        return text, spoken

    async def send_voice(self, chat, spoken, reply_to=None):
        """Speak (lang, text) as a voice message. Returns None, or a one-line note when it failed; never raises."""
        lang, text = spoken
        text = voicemod.cap(redaction.redact(text)[0])
        try:
            data = await voicemod.synthesize(text, self.voice_cfg["voices"][lang])
            await self.app.bot.send_voice(chat, io.BytesIO(data), filename="reply.ogg", **reply_kw(reply_to))
        except voicemod.VoiceError as e:
            return voicemod.NOTE_PREFIX + str(e)
        except Exception as e:
            log.warning("[%s] sending the voice message failed: %s", self.name, type(e).__name__)
            return voicemod.NOTE_PREFIX + type(e).__name__
        return None

    def context_status(self, chat):
        c = self.ctx.get(chat) or {}
        if c.get("pct") is None or chat not in self.sessions:
            return []
        tok = f" ({c['tokens'] // 1000}k of {c['window'] // 1000}k tokens)" if c.get("tokens") and c.get("window") else ""
        return [f"Context: {c['pct']}%{tok}"]

    async def download(self, ctx, file_id, name):
        settings.INBOX.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^\w.\-]", "_", name)
        path = settings.INBOX / f"{datetime.now():%Y%m%d-%H%M%S}-{safe}"
        f = await ctx.bot.get_file(file_id)
        await f.download_to_drive(path)
        return path

    async def process_video(self, msg, path):
        """Frames + transcript for a downloaded video. Returns (frame paths, prompt suffix). Never raises."""
        frames, transcript, note, dur = [], "", None, None
        status = None
        try:
            status = await msg.reply_text("🎬 Processing video…")
        except Exception:
            pass
        try:
            if not await asyncio.to_thread(videomod.have_ffmpeg):
                note = "ffmpeg not found: only the file is passed on"
            else:
                dur = await asyncio.to_thread(videomod.probe, path)
                n = videomod.frame_count(dur, self.g.get("video_frames", 4))
                if n:
                    frames = await asyncio.to_thread(videomod.extract_frames, path, path.parent, n, dur)
                if self.g.get("video_transcribe", True):
                    wav = await asyncio.to_thread(videomod.extract_audio, path, path.parent)
                    if wav:
                        try:
                            transcript = await media.transcribe(wav, self.g["whisper_model"])
                        except ImportError:
                            note = "voice not enabled: no transcript"
                        except Exception as e:
                            log.warning("[%s] video transcription failed: %s", self.name, type(e).__name__)
                        finally:
                            wav.unlink(missing_ok=True)
        except Exception:
            log.exception("[%s] video processing failed", self.name)
        if status:
            try:
                await status.delete()
            except Exception:
                pass
        head = " · ".join(p for p in (
            videomod.fmt_duration(dur) if dur else "",
            f"{len(frames)} frame{'s' if len(frames) != 1 else ''}" if frames else "") if p)
        text = ("🎬 " + (head or "video received") + (f"\n🎙️ {transcript}" if transcript else "")
                + (f"\n{note}" if note else ""))
        try:
            await self.say(msg, text)
        except Exception as e:
            log.warning("[%s] video summary reply failed: %s", self.name, type(e).__name__)
        extra = ""
        if transcript:
            extra += f"\nTranscript of the audio: {transcript}"
        if frames:
            extra += "\nStill frames (in order): " + ", ".join(str(f) for f in frames)
        return frames, extra

    async def build_prompt(self, update, ctx, text):
        """Download/transcribe the message and decide the mode. Returns a dict
        {mode, prompt (for the agent), text (for chat), images} or None."""
        msg = update.effective_message
        images, has_file, path, caption = [], False, None, text
        vid = (getattr(msg, "video", None) or getattr(msg, "video_note", None)
               or getattr(msg, "animation", None))
        extra = ""
        try:
            if msg.voice or msg.audio:
                m = msg.voice or msg.audio
                name = getattr(m, "file_name", None) or "voice.ogg"
                path = await self.download(ctx, m.file_id, name)
                status = await msg.reply_text("🎙️ Transcribing…")
                tr = await media.transcribe(path, self.g["whisper_model"])
                path.unlink(missing_ok=True)   # voice note is not needed once transcribed
                try:
                    await status.delete()
                except Exception:
                    pass
                if not tr:
                    await msg.reply_text("🎙️ (no speech detected)")
                    return None
                await self.say(msg, f"🎙️ {tr}")
                caption, path = tr, None
            elif vid:
                if (getattr(vid, "file_size", None) or 0) > MAX_DOWNLOAD:
                    await msg.reply_text("🎬 Video is too big for Telegram bots (max 20 MB). Send a shorter clip.")
                    return None
                path = await self.download(ctx, vid.file_id, getattr(vid, "file_name", None) or "video.mp4")
                images, extra = await self.process_video(msg, path)
                has_file = True
            elif msg.photo:
                path = await self.download(ctx, msg.photo[-1].file_id, "photo.jpg")
                images = [path]
            elif msg.document:
                d = msg.document
                path = await self.download(ctx, d.file_id, d.file_name or "file")
                if (d.mime_type or "").startswith("image/"):
                    images = [path]
                else:
                    has_file = True
        except Exception as e:
            log.exception("[%s] media handling failed", self.name)
            if "too big" in str(e).lower():
                await self.say(msg, "🎬 Video is too big for Telegram bots (max 20 MB). Send a shorter clip.")
            else:
                await self.say(msg, f"Media handling failed: {e}")
            return None
        if not caption and not path:
            return None
        if self.chat_enabled:
            mode, caption = chatmod.route(caption or "", self.mode, bool(images), has_file,
                                           self.last_mode.get(update.effective_chat.id)
                                           if update.effective_chat.id in self.sessions else None,
                                           self.agent_triggers)
        else:
            mode, caption = "agent", caption or ""
        if vid:
            prompt = f"{caption or 'Look at this video'}\n\nVideo file: {path}{extra}"
        elif images:
            prompt = f"{caption or 'Look at this image'}\n\nImage file: {path}"
        elif has_file:
            prompt = f"{caption or 'Look at this file'}\n\nFile: {path}"
        else:
            prompt = caption
        return {"mode": mode, "prompt": prompt, "text": caption, "images": images}

    # ---- worker
    async def work(self):
        while True:
            chat, uid, item = await self.queue.get()
            if self.shutting_down:   # leave it in the journal queue for /resume
                return
            reply_to = item.get("msg_id")
            item.setdefault("uid", uid)
            item.setdefault("jid", uuid.uuid4().hex)
            jid = item["jid"]
            try:
                if not self.auth.is_unlocked(uid):
                    self.journal.discard(self.name, chat, jid)
                    await self.send(chat, "🔒 Locked; queued task dropped.", reply_to)
                    continue
                self.journal.start(self.name, chat, self.record(chat, uid, item))
                try:
                    await self.run_one(chat, item)
                    if not self.shutdown_killed:   # only a run killed by the shutdown keeps its record
                        self.journal.finish(self.name, chat, jid)
                except asyncio.CancelledError:
                    raise   # cancelled (shutdown): the record stays
                except BaseException:
                    if not self.shutdown_killed:
                        self.journal.finish(self.name, chat, jid)
                    raise
                finally:
                    self.auth.touch(uid)   # a long task counts as activity: the idle lock must not drop what is queued
            except Exception:
                log.exception("[%s] task failed", self.name)
                try:
                    await self.send(chat, "Internal error; see bridge.log.", reply_to)
                except Exception:
                    pass
            finally:
                self.current = None

    async def run_one(self, chat, item):
        self.cold_start = False
        if self.qwen:
            if not await self.qwen_start(chat):
                return
            self.qwen.busy += 1
        try:
            if item["mode"] == "chat":
                await self.run_chat(chat, item)
            else:
                prompt = item["prompt"]
                if self.chat_enabled:
                    prompt = chatmod.history_prefix(self.unseen.pop(chat, [])) + prompt
                if chat in self.voice_on and not item.get("save"):
                    prompt += "\n\n" + voicemod.VOICE_MODE_NOTE
                if item.get("save"):   # background memory save: must not make the next chat turn agent-sticky
                    await self.run_task(chat, prompt, None, "(automatic memory save)")
                    self.last_mode[chat] = "chat"
                else:
                    await self.run_task(chat, prompt, item.get("msg_id"), item.get("orig_prompt") or item["prompt"],
                                        session_id=item.get("session_id"))
        finally:
            if self.qwen:
                self.qwen.busy -= 1
                self.qwen.touch()

    async def _typing_loop(self, chat):
        while True:
            try:
                await self.app.bot.send_chat_action(chat, "typing")
            except Exception:
                pass
            await asyncio.sleep(TYPING_INTERVAL_S)

    async def run_chat(self, chat, item):
        """Fast path: one direct chat-completion call (no agent loop, no tools)."""
        reply_to = item.get("msg_id")
        self.current = (chat, item["text"][:60].replace("\n", " "))
        system = chatmod.build_system(self.cfg.get("chat_system_prompt", ""), self.memory_dir())
        if self.voice_cfg:
            system += "\n\n" + voicemod.convention_sentence(self.voice_cfg["voices"])
        text = item["text"] or "Look at this image"
        content = text
        if item["images"]:
            imgs = item["images"]
            if len(imgs) > MAX_CHAT_IMAGES:   # video frames: the local model gets only the first and the middle one
                imgs = [imgs[0], imgs[len(imgs) // 2]]
            content = [{"type": "text", "text": text}] + [chatmod.image_part(p) for p in imgs]
        messages = self.chats.messages(chat, system, content)
        if chat in self.voice_on:   # the note goes to the model only, not into the saved history
            last = messages[-1]["content"]
            note = "\n\n" + voicemod.VOICE_MODE_NOTE
            last = last + note if isinstance(last, str) else [{**last[0], "text": last[0]["text"] + note}, *last[1:]]
            messages = [*messages[:-1], {**messages[-1], "content": last}]
        t0 = time.monotonic()
        typing = asyncio.create_task(self._typing_loop(chat))   # "typing…" stays visible for the whole wait
        try:
            reply = await chatmod.complete(self.local_url, messages, model=self.cfg["chat_model"],
                                           max_tokens=int(self.cfg.get("chat_max_tokens", 2048)))
        except Exception as e:
            self.chats.drop_last_user(chat)
            log.error("[%s] chat call failed: %s", self.name, type(e).__name__)
            await self.send(chat, f"⚠️ {self.display} (chat mode) failed: {type(e).__name__}. "
                                  "Try again, or prefix with ! for agent mode.", reply_to)
            return
        finally:
            typing.cancel()
        log.info("[%s] chat reply in %.1f s (images=%d)", self.name, time.monotonic() - t0, len(item["images"]))
        reply, spoken = self.voice_split(chat, reply)
        reply = reply or ("🔊" if spoken else "(empty response)")
        dropped = self.chats.add_reply(chat, reply, text)
        self.last_mode[chat] = "chat"
        self.unseen.setdefault(chat, []).extend([("user", text), ("assistant", reply)])
        del self.unseen[chat][:-12]
        notify_trim = bool(dropped) and not self.ctx_entry(chat)["trimmed"]
        if notify_trim:
            self.ctx[chat]["trimmed"] = True
        self.save_state()
        saving = (self.memory_enabled() and bool(item.get("uid")) and not item["images"]
                  and chatmod.wants_save(text, self.save_triggers))
        if saving:   # say it honestly in the same message: the save runs afterwards in the agent
            reply += "\n\n" + (self.cfg["memory"].get("notice_text") or settings.DEFAULT_MEMORY_NOTICE)
        if spoken and (note := await self.send_voice(chat, spoken, reply_to)):
            reply = note if reply == "🔊" else reply + "\n\n" + note
        await self.send_answer(chat, reply, reply_to)
        if saving:
            await self.enqueue(chat, item["uid"], {"mode": "agent", "prompt": self.cfg["memory"]["save_prompt"], "text": "",
                                                       "images": [], "save": True})
        if notify_trim:
            await self.send(chat, ctxinfo.trimmed_notice(self.chats.max_messages))

    def memory_enabled(self):
        mem = self.cfg.get("memory") or {}
        return bool(mem.get("enabled") and mem.get("dir") and mem.get("save_prompt"))

    def memory_dir(self):
        """Notes folder added to the chat system prompt, or None when memory is off."""
        mem = self.cfg.get("memory") or {}
        return Path(mem["dir"]) if mem.get("enabled") and mem.get("dir") else None

    def status_text(self, elapsed, steps, tool_line, sub_lines=(), done=0):
        head = f"⏳ {self.display} is working… {fmt_elapsed(elapsed)}" + (f" · step {steps}" if steps else "")
        lines = [head] + ([tool_line] if tool_line else []) + list(sub_lines) + ([f"✓ {done} done"] if done else [])
        return "\n".join(lines)

    async def run_task(self, chat, prompt, reply_to=None, user_text=None, session_id=None):
        bot = self.app.bot
        self.current = (chat, prompt[:60].replace("\n", " "))
        self.runner.cwd = self.cwd_for(chat)
        prev_sid = session_id or self.sessions.get(chat)
        idle = self.idle_reset_due(chat) if session_id is None and prev_sid else None
        if idle is not None:   # the cache expired: resuming would re-read the whole session at full price
            tokens = self.ctx[chat].get("tokens")
            self.parked[chat] = {"sid": self.sessions.pop(chat), "ctx": self.ctx.pop(chat)}
            prev_sid = None
            self.save_state()
            await self.send(chat, ctxinfo.idle_reset_notice(idle, tokens), reply_to)
        if self.qwen and self.cold_start and prev_sid:
            tokens = (self.ctx.get(chat) or {}).get("tokens") or 0
            if tokens >= ctxinfo.LARGE_SESSION_TOKENS:
                await self.send(chat, ctxinfo.large_session_notice(tokens), reply_to)
        self.cold_start = False
        t0 = time.monotonic()
        first = f"⏳ {self.display} is working…"
        status = await bot.send_message(chat, first, **reply_kw(reply_to))
        interval = float(self.g["edit_interval_seconds"])
        st = {"edit": t0, "text": first, "steps": 0, "tool": ""}
        subs = progress.Tracker(time.monotonic, fmt_tool)

        async def refresh(force=False):
            now = time.monotonic()
            if not force and now - st["edit"] < interval:
                return
            text, _ = redaction.redact(self.status_text(now - t0, st["steps"], st["tool"], subs.lines(now), subs.done))
            if text == st["text"]:
                return
            st["edit"], st["text"] = now, text
            try:
                await status.edit_text(text)
            except BadRequest as e:
                if "not modified" not in str(e).lower():
                    log.debug("[%s] status edit failed: %s", self.name, e)
            except Exception:
                pass

        async def on_event(ev):
            changed = subs.feed(ev)
            if ev["kind"] == "tool":
                st["steps"] += 1
                st["tool"] = fmt_tool(ev)
                changed = True
            if changed:
                await refresh()

        async def heartbeat():
            while True:   # refresh at least every heartbeat_s, even without tool events
                await asyncio.sleep(max(0.05, st["edit"] + self.heartbeat_s - time.monotonic()))
                await refresh()

        log.info("[%s] run chat=%s resume=%s", self.name, chat, bool(prev_sid))
        beat = asyncio.create_task(heartbeat())
        try:
            final, sid, is_err = await self.runner.run(prompt, prev_sid, on_event)
        finally:
            beat.cancel()
        stats = self.runner.stats
        usable = not (self.runner.stopped or self.runner.timed_out)
        pct = ctxinfo.percent(stats.context_tokens, stats.context_window) if usable else None
        notes = []
        if sid and not is_err:
            if sid != prev_sid:
                self.ctx.pop(chat, None)     # a new session id: forget flags/usage of the old one
            self.sessions[chat] = sid
            self.ctx_entry(chat)["last"] = self.wall()
            if pct is not None:
                e = self.ctx_entry(chat)
                warned = set(e["warned"])
                notes = ([ctxinfo.COMPACTED_NOTICE] if stats.compacted else []) + ctxinfo.warnings(pct, warned)
                e.update(tokens=stats.context_tokens, window=stats.context_window, pct=pct, warned=sorted(warned))
            self.save_state()
        try:
            await status.delete()
        except Exception:
            pass
        spoken = None
        if not is_err:
            final, spoken = self.voice_split(chat, final)
        final = final or ("🔊" if spoken else "(empty response)")
        if self.chat_enabled and not is_err:   # chat mode must see agent turns too
            self.chats.add_turn(chat, user_text or prompt, final)
            self.last_mode[chat] = "agent"
            self.save_state()
        if is_err:
            final = "⚠️ " + final
        footer = ctxinfo.footer(pct)
        files = []
        if self.outgoing_files:
            final, items = outfiles.extract(final)
            allowed = self.allowed_dirs()
            for raw, cap in items:
                path, why = outfiles.check(raw, allowed, [], base=self.cwd_for(chat))
                if path is None:
                    final = (final + "\n" if final else "") + f"{outfiles.note_prefix()}{outfiles._name(raw)} ({why})"
                else:
                    files.append((raw, cap))
            final = final or ("📎" if files else "(empty response)")
        if spoken and (note := await self.send_voice(chat, spoken, reply_to)):
            final = note if final == "🔊" else final + "\n\n" + note
        await self.send_answer(chat, final, reply_to, footer)
        if files:
            for n in await outfiles.send(bot, chat, files, self.allowed_dirs(), [], reply_to,
                                          base=self.cwd_for(chat)):
                await self.send(chat, n)
        for n in notes:
            await self.send(chat, n)

    # ---- pinned lock status
    async def update_pin(self, uid):
        """Edit (or send + pin) the lock-status message of one user when its text changed. Never raises."""
        if not (self.pin_enabled and self.pin_active and self.app):
            return
        async with self._pin_lock:
            try:
                text, _ = redaction.redact(statuspin.status_text(self.auth, uid))
                if self._pin_text.get(uid) == text:
                    return
                bot, mid = self.app.bot, self.pin_store.get(self.name, uid)
                if mid is not None:
                    try:
                        await bot.edit_message_text(chat_id=uid, message_id=mid, text=text)
                        self._pin_text[uid] = text
                        return
                    except BadRequest as e:
                        low = str(e).lower()
                        if "not modified" in low:
                            self._pin_text[uid] = text
                            return
                        if "not found" not in low and "message to edit" not in low:
                            raise
                sent = await bot.send_message(uid, text, disable_notification=True)
                self.pin_store.set(self.name, uid, sent.message_id)
                await bot.pin_chat_message(uid, sent.message_id, disable_notification=True)
                self._pin_text[uid] = text
            except Exception as e:
                log.warning("[%s] status pin for %s failed: %s", self.name, uid, type(e).__name__)

    async def pin_loop(self):
        while True:
            for uid in sorted(self.auth.allowed):
                await self.update_pin(uid)
            await asyncio.sleep(statuspin.INTERVAL_S)

    # ---- lifecycle
    def on_polling_error(self, err) -> None:
        if isinstance(err, Conflict):
            if not getattr(self, "_conflict_logged", False):
                self._conflict_logged = True
                log.error("[%s] another program is polling this bot token (409 Conflict). "
                          "Stop the other copy or use a different token.", self.name)
            return
        now = time.monotonic()
        last = self.__dict__.setdefault("_poll_err_at", {})
        key = type(err).__name__
        if key not in last or now - last[key] >= 60:
            last[key] = now
            log.warning("[%s] polling error: %s: %s", self.name, key, err)

    async def start(self):
        self.app = Application.builder().token(self.token).build()
        self.app.add_handler(MessageHandler(filters.ALL & ~filters.StatusUpdate.ALL, self.on_update))
        await self.app.initialize()
        await self.app.bot.set_my_commands([BotCommand(c, d) for c, d in commands_for(self.cfg)])
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True, error_callback=self.on_polling_error)
        self.worker = asyncio.create_task(self.work())
        me = await self.app.bot.get_me()
        log.info("[%s] started as @%s", self.name, me.username)
        await self.announce_online()
        if self.pin_enabled:
            self.pin_active = True
            self.pin_task = asyncio.create_task(self.pin_loop())
        await self.offer_resume()

    async def announce_online(self):
        """Tell every private chat that has a persisted session/history that the context survived the restart.
        Content-free, so it is fine while locked."""
        chats = set(self.sessions) | {c for c, h in self.chats.history.items() if h}
        for chat in sorted(chats):
            if not self.auth.is_allowed(chat):   # private chats only: chat id == allowed user id
                continue
            pct = (self.ctx.get(chat) or {}).get("pct") if chat in self.sessions else None
            text = f"🟢 {self.display} is back online. Context kept" + (f" ({pct}%)." if pct is not None else ".")
            try:
                await self.app.bot.send_message(chat, text)
            except Exception as e:
                log.warning("[%s] startup notice to %s failed: %s", self.name, chat, type(e).__name__)

    async def stop(self):
        self.shutting_down = True
        if self.app and self.runner.running and self.current:
            try:
                await asyncio.wait_for(self.app.bot.send_message(self.current[0], RESTART_NOTICE), 5)
            except Exception:
                pass
        if self.runner.running:
            self.shutdown_killed = True
        self.runner.stop()
        if self.worker:
            self.worker.cancel()
        if self.pin_task:
            self.pin_task.cancel()
        if self.app:
            try:
                if self.app.updater.running:
                    await self.app.updater.stop()
                if self.app.running:
                    await self.app.stop()
                await self.app.shutdown()
            except Exception:
                log.exception("[%s] shutdown error", self.name)


async def main():
    cfg = settings.load_config()
    env, ids = settings.load_secrets()
    g = cfg["general"]
    secret_values = redaction.secret_values(env)
    redaction.set_known(secret_values)
    settings.OUTBOX.mkdir(parents=True, exist_ok=True)
    setup_logging([v for v in secret_values if len(v) >= redaction.MIN_KNOWN_LEN])
    auth = Auth(ids, env.get("TOTP_SECRET", ""), g["session_hours"], g["max_failures"], g["lockout_minutes"],
                state_path=settings.STATE_DIR / "state.json",
                idle_lock_hours=float(g.get("idle_lock_hours", 3)))
    if g.get("whisper_preload", True):
        media.preload(g["whisper_model"])   # background thread: never delays bot startup
    try:
        qwen = make_local_manager(cfg)
        local_ok = True
    except (ValueError, KeyError) as e:
        log.error("local model disabled: %s", e)
        qwen, local_ok = None, False
    idle_task = asyncio.create_task(qwen.idle_loop()) if qwen else None
    inbox_task = asyncio.create_task(housekeeping.inbox_loop(settings.INBOX, float(g.get("inbox_retention_days", 7))))
    store = StateStore(settings.STATE_DIR / "sessions.json")
    jrn = journalmod.Journal(settings.STATE_DIR / "inflight.json")
    services = {}
    warned = set()
    try:
        while True:
            env, ids = settings.load_secrets()   # re-read every 60 s: filling in .env is enough
            auth.allowed = set(ids)
            if env.get("TOTP_SECRET"):
                auth.secret = env["TOTP_SECRET"]
            else:
                warned_once(warned, "totp", "TOTP_SECRET empty: run setup.py")
            if not ids:
                warned_once(warned, "ids", "ALLOWED_USER_IDS is empty: only /whoami works until you set it.")
            for name, bcfg in cfg["bots"].items():
                if name in services or (bcfg.get("needs_local_model") and not local_ok):
                    continue
                token = env.get(bcfg["token_env"], "")
                if not token:
                    warned_once(warned, name, f"[{name}] {bcfg['token_env']} is missing in "
                                f"{settings.STATE_DIR / '.env'}; waiting (re-checking every 60 s)")
                    continue
                svc = BotService(name, bcfg, token, auth, g, qwen, store=store, local_url=cfg["local_model"].get("url"), journal=jrn)
                try:
                    await svc.start()
                    services[name] = svc
                except Exception as e:
                    log.error("[%s] failed to start: %s", name, type(e).__name__)
                    await svc.stop()
            await asyncio.sleep(60)
    finally:
        inbox_task.cancel()
        if idle_task:
            idle_task.cancel()
        for svc in services.values():
            await svc.stop()


def warned_once(seen, key, msg):
    if key not in seen:
        seen.add(key)
        log.warning(msg)

