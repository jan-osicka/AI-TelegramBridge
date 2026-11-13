"""Context-usage helpers: percentage, footer line, one-time warnings."""

WARN_LEVELS = (70, 90)


def percent(tokens, window):
    """Whole-number percent of the window used, or None if unknown."""
    if not tokens or not window:
        return None
    return int(round(100 * tokens / window))


def footer(pct, cost=None, show_cost=False):
    """`· 42% context` plus ` · $0.18` (Claude only, never for the local model). Empty if the percentage is unknown."""
    if pct is None:
        return ""
    s = f"· {pct}% context"
    if show_cost and cost and cost > 0:
        s += f" · ${cost:.2f}"
    return s


def warnings(pct, warned):
    """One-time warnings. `warned` is the set of levels already sent (mutated). Returns a list of texts.
    Crossing 90% straight away sends only the stronger warning."""
    out = []
    if pct is None:
        return out
    warned.intersection_update(l for l in WARN_LEVELS if pct >= l)   # context shrank (compaction): re-arm
    for level in sorted(WARN_LEVELS, reverse=True):
        if pct >= level and level not in warned:
            warned.update(l for l in WARN_LEVELS if l <= level)
            if level >= 90:
                out.append(f"🚨 Context {pct}% full. Claude will auto-compact very soon and details from earlier "
                           "in this conversation will be summarized or lost. Send /exit for a fresh start "
                           "unless you need that history.")
            else:
                out.append(f"⚠️ Context {pct}% full. Claude will auto-compact soon (older details get summarized). "
                           "Send /exit for a fresh start if the topic changed.")
            break
    return out


COMPACTED_NOTICE = "ℹ️ Context was compacted (older parts summarized)."
LARGE_SESSION_TOKENS = 30000


def trimmed_notice(keep=24):
    return f"ℹ️ Older chat messages dropped from my memory (keeps the last {keep})."


def large_session_notice(tokens):
    return (f"⏳ Large session (~{round(tokens / 1000)}k tokens): the first reply after a restart re-reads it, "
            f"~{round(tokens / 2300)} s. /exit for a fresh start.")
