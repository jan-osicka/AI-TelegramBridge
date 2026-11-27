"""Runs the agent CLI and parses stream-json output."""
import asyncio
import json
import os
from dataclasses import dataclass

from . import osutil
from . import redaction


def parse_event(line):
    """Parse one stream-json line. Returns dict with kind in:
    session (session_id), tool (name, detail), text (text), result (text, session_id, is_error); or None."""
    line = line.strip()
    if not line:
        return None
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(ev, dict):
        return None
    t = ev.get("type")
    if t == "system" and ev.get("subtype") == "compact_boundary":
        meta = ev.get("compact_metadata") or {}
        return {"kind": "compact", "pre_tokens": _int(meta.get("pre_tokens") if isinstance(meta, dict) else None)}
    if t == "system" and ev.get("session_id"):
        return {"kind": "session", "session_id": ev["session_id"]}
    if t == "assistant":
        msg = ev.get("message")
        msg = msg if isinstance(msg, dict) else {}
        extra = {"ctx": context_tokens(msg.get("usage")), "model": msg.get("model"),
                 "sub": ev.get("parent_tool_use_id") is not None}   # sub = sub-agent message
        blocks = msg.get("content") or []
        if not isinstance(blocks, list):
            return {"kind": "usage", **extra} if extra["ctx"] else None
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                inp = b.get("input") or {}
                detail = (inp.get("command") or inp.get("file_path") or inp.get("pattern")
                          or inp.get("path") or inp.get("url") or inp.get("description") or "")
                return {"kind": "tool", "name": b.get("name", "?"), "detail": redaction.redact(str(detail))[0][:80],
                        "parent": ev.get("parent_tool_use_id"), **extra}
        texts = [b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
        if texts:
            return {"kind": "text", "text": "".join(texts), **extra}
        return {"kind": "usage", **extra} if extra["ctx"] else None
    if t == "result":
        windows = {}
        mu = ev.get("modelUsage")
        if isinstance(mu, dict):
            for m, u in mu.items():
                w = _int(u.get("contextWindow")) if isinstance(u, dict) else None
                if w:
                    windows[m] = w
        cost = ev.get("total_cost_usd")
        return {"kind": "result", "text": ev.get("result") or "",
                "session_id": ev.get("session_id"), "is_error": bool(ev.get("is_error")),
                "cost_usd": float(cost) if isinstance(cost, (int, float)) else None, "windows": windows}
    return None


AGENT_TOOLS = ("Agent", "Task")


def parse_events(line):
    """All events of one stream-json line (pure). parse_event's event first, then one subagent_start per
    main-agent Agent/Task tool_use (parallel subagents), or one subagent_end per tool_result id of a
    main-agent user message (the tracker ignores ids it does not know)."""
    # A background-launched Agent returns its tool_result at once, so it is counted as done right away.
    first = parse_event(line)
    out = [first] if first else []
    try:
        ev = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return out
    if not isinstance(ev, dict) or ev.get("parent_tool_use_id") is not None:
        return out
    msg = ev.get("message")
    blocks = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(blocks, list):
        return out
    for b in blocks:
        if not isinstance(b, dict):
            continue
        if ev.get("type") == "assistant" and b.get("type") == "tool_use" and b.get("name") in AGENT_TOOLS and b.get("id"):
            inp = b.get("input") if isinstance(b.get("input"), dict) else {}
            desc = redaction.redact(str(inp.get("description") or ""))[0]
            out.append({"kind": "subagent_start", "id": b["id"], "description": desc,
                        "model": inp.get("model") or None, "agent_type": inp.get("subagent_type") or None})
        elif ev.get("type") == "user" and b.get("type") == "tool_result" and b.get("tool_use_id"):
            out.append({"kind": "subagent_end", "id": b["tool_use_id"]})
    return out


def _int(v):
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def context_tokens(usage):
    """Context size of one API call: input + cache_read + cache_creation tokens (None if unknown/zero)."""
    if not isinstance(usage, dict):
        return None
    total = sum(_int(usage.get(k)) or 0 for k in
                ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    return total or None


@dataclass
class RunStats:
    """What the last run() learned about context and cost."""
    context_tokens: int | None = None
    context_window: int | None = None
    cost_usd: float | None = None
    compacted: bool = False
    model: str | None = None


def build_cmd(bot_cfg, prompt, session_id=None):
    def sub(a):
        return a.replace("{prompt}", prompt).replace("{session_id}", session_id or "")
    cmd = [bot_cfg["command"]] + [sub(a) for a in bot_cfg["args"]]
    if session_id:
        cmd += [sub(a) for a in bot_cfg.get("resume_args", [])]
    extra = (bot_cfg.get("append_system_prompt") or "").strip()
    if extra:
        cmd += ["--append-system-prompt", extra]
    return cmd


def uses_stdin(bot_cfg):
    """The prompt goes to stdin unless the args still contain a {prompt} placeholder."""
    return not any("{prompt}" in a for a in bot_cfg.get("args", []))


kill_tree = osutil.kill_tree
ANSWER_GRACE_S = 3.0   # a result while the process lives on this long means background work is still running


class Runner:
    def __init__(self, bot_cfg, cwd, timeout_s=None):
        self.cfg = bot_cfg
        self.cwd = cwd
        self.timeout_s = timeout_s   # None or 0 = no limit
        self.proc = None
        self.stopped = False
        self.timed_out = False
        self.stats = RunStats()      # context/cost of the last run()

    @property
    def running(self):
        return self.proc is not None and self.proc.returncode is None

    async def run(self, prompt, session_id=None, on_event=None):
        """Returns (final_text, session_id, is_error). A run with background work has several results: the earlier
        ones go to on_event as kind "answer"; final_text is None when nothing is left to deliver after them."""
        stdin_mode = uses_stdin(self.cfg)
        cmd = build_cmd(self.cfg, prompt, session_id)
        env = os.environ.copy()
        env.update(self.cfg.get("env", {}) or {})
        self.stopped = self.timed_out = False
        self.proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=self.cwd, env=env,
            stdin=asyncio.subprocess.PIPE if stdin_mode else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=64 * 1024 * 1024, **osutil.spawn_kwargs())
        stderr_task = asyncio.create_task(self.proc.stderr.read())
        state = {"sid": session_id, "final": None, "is_error": False, "last_text": "",
                 "ctx": None, "model": None, "compacted": False, "cost": None, "windows": {}, "answered": False}
        self.stats = RunStats()
        err = ""
        try:
            try:
                async with asyncio.timeout(self.timeout_s or None):
                    if stdin_mode:
                        try:
                            self.proc.stdin.write(prompt.encode("utf-8"))
                            await self.proc.stdin.drain()
                            self.proc.stdin.close()
                        except (ConnectionError, OSError):
                            pass  # process died early; the output loop reports it
                    await self._read_events(state, on_event)
                    await self.proc.wait()
            except TimeoutError:
                self.timed_out = True
                await osutil.terminate_tree(self.proc)
            err = (await stderr_task).decode("utf-8", "replace").strip()
        finally:
            if self.proc.returncode is None:
                await osutil.terminate_tree(self.proc)
        sid, final = state["sid"], state["final"]
        self.stats = self._make_stats(state)
        if self.stopped:
            return "Stopped.", sid, True
        if self.timed_out:
            mins = self.timeout_s / 60
            return f"Task timed out after {mins:g} min and was stopped.", sid, True
        if final is None:
            if state["answered"] and not state["last_text"] and self.proc.returncode == 0:
                return None, sid, False
            final = state["last_text"] or f"Agent exited with code {self.proc.returncode}.\n{err[-1500:]}"
            return final, sid, True
        return final, sid, state["is_error"]

    async def _read_events(self, state, on_event):
        loop = asyncio.get_running_loop()
        deadline = None   # when a held result goes out although the process is still running
        while True:
            try:
                async with asyncio.timeout_at(deadline):
                    raw = await self.proc.stdout.readline()
            except TimeoutError:
                deadline = None
                await self._flush_answer(state, on_event)
                continue
            if not raw:
                break
            for ev in parse_events(raw.decode("utf-8", "replace")):
                if ev["kind"] == "result":
                    await self._flush_answer(state, on_event)
                    deadline = loop.time() + ANSWER_GRACE_S
                await self._handle(ev, state, on_event)

    async def _flush_answer(self, state, on_event):
        """Deliver the held result now, so a later result of the same run does not replace it."""
        if state["final"] is None or not on_event:
            return
        ev = {"kind": "answer", "text": state["final"], "session_id": state["sid"], "is_error": state["is_error"]}
        self.stats = self._make_stats(state)
        state.update(final=None, is_error=False, last_text="", answered=True, compacted=False)
        await on_event(ev)

    async def _handle(self, ev, state, on_event):
        if ev.get("ctx") and not ev.get("sub"):   # last main-agent API call = current context size
            state["ctx"], state["model"] = ev["ctx"], ev.get("model") or state["model"]
        if ev["kind"] == "session":
            state["sid"] = ev["session_id"]
        elif ev["kind"] == "text" and not ev.get("sub"):   # subagent text is never the answer
            state["last_text"] = ev["text"]
        elif ev["kind"] == "compact":
            state["compacted"] = True
        elif ev["kind"] == "result":
            state["final"], state["is_error"] = ev["text"], ev["is_error"]
            state["sid"] = ev["session_id"] or state["sid"]
            state["cost"], state["windows"] = ev["cost_usd"], ev["windows"]
        if on_event:
            await on_event(ev)

    def _make_stats(self, state):
        """Window: result.modelUsage[main model].contextWindow, else config `context_window`.
        context_window_source = "config" ignores modelUsage (for models Claude Code does not know)."""
        window = None
        if (self.cfg.get("context_window_source") or "auto") != "config":
            m, ws = state["model"], state["windows"]
            window = ws.get(m) or next((w for k, w in ws.items() if m and (k.startswith(m) or m.startswith(k))), None)
        window = window or _int(self.cfg.get("context_window")) or None
        cost = state["cost"] if state["cost"] and state["cost"] > 0 else None
        return RunStats(state["ctx"], window, cost, state["compacted"], state["model"])

    def stop(self):
        if self.running:
            self.stopped = True
            kill_tree(self.proc.pid)
            try:   # escalate to SIGKILL without blocking the loop
                self._escalate = asyncio.get_running_loop().create_task(osutil.escalate(self.proc))
            except RuntimeError:
                pass
            return True
        return False
