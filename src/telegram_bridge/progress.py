"""Live subagent progress: which subagents run, what each did last, how many finished."""

MAX_SHOWN = 5


def default_fmt(ev):
    d = (ev.get("detail") or "").replace("\n", " ")
    return f"🔧 {ev.get('name', '?')}: {d}" if d else f"🔧 {ev.get('name', '?')}"


class Tracker:
    def __init__(self, clock, fmt=default_fmt):
        self.clock, self.fmt = clock, fmt
        self.running = {}    # agent tool_use id -> {description, label, t0, last}
        self.done = 0

    def feed(self, ev):
        """Returns True if what lines() shows may have changed."""
        kind = ev.get("kind")
        if kind == "subagent_start":
            sid = ev.get("id")
            if not sid or sid in self.running:
                return False
            label = ev.get("model") or ev.get("agent_type") or ""
            self.running[sid] = {"description": ev.get("description") or "", "label": label,
                                 "t0": self.clock(), "last": ""}
            return True
        if kind == "tool" and ev.get("parent"):
            entry = self.running.get(ev["parent"])
            if entry is None:
                return False
            entry["last"] = self.fmt(ev)
            return True
        if kind == "subagent_end":
            if self.running.pop(ev.get("id"), None) is None:
                return False
            self.done += 1
            return True
        return False

    def lines(self, now):
        out = []
        for e in list(self.running.values())[:MAX_SHOWN]:
            secs = max(0, int(now - e["t0"]))
            parts = [e["description"][:40], e["label"], f"{secs // 60}:{secs % 60:02d}", e["last"]]
            out.append("↳ " + " · ".join(p for p in parts if p))
        extra = len(self.running) - MAX_SHOWN
        if extra > 0:
            out.append(f"+{extra} more")
        return out
