"""Fast chat mode for the local-model bot: direct /v1/chat/completions calls (no Claude Code agent loop),
plus the routing between "chat" and "agent" modes."""
import asyncio
import base64
import json
import re
import unicodedata
import urllib.request
from pathlib import Path

MODES = ("agent", "chat", "auto")
BRAIN_MAX_CHARS = 32000  # ~10k tokens; benchmarked: a warm (cached) prompt costs ~0 s extra, window is 131k

# Words (accent-insensitive, Czech and English stems) that mean "do something on the PC".
_ACTION = [
    r"soubor\w*", r"slozk\w*", r"adresar\w*", r"file\w*", r"folder\w*", r"director\w*",
    r"spust\w*", r"spoust\w*", r"\bran\b", r"\brun\w*", r"prikaz\w*", r"command\w*", r"terminal\w*", r"powershell", r"\bcmd\b",
    r"nainstaluj\w*", r"instal\w*", r"odinstal\w*", r"\bpip\b", r"\bnpm\b", r"\bwinget\b",
    r"kod\w*", r"\bcode\b", r"skript\w*", r"script\w*", r"program\w*", r"funkc\w*", r"\bbug\w*", r"\btest\w*", r"refaktor\w*",
    r"\bgit\w*", r"commit\w*", r"\bpush\w*", r"\bpull\b", r"repo\w*",
    r"otevri\w*", r"\bopen\b", r"vytvor\w*", r"\bcreate\b", r"napis\w*\s+(?:do|soubor|skript|kod)", r"\bwrite\s+(?:a|the|to)\b",
    r"smaz\w*", r"\bdelete\w*", r"\bremove\b", r"uprav\w*", r"\bedit\w*", r"zmen\w*\s+(?:v|ve|soubor|kod)", r"prejmenuj\w*", r"\brename\w*",
    r"stahni\w*", r"stahn\w*", r"download\w*", r"\bweb\w*", r"internet\w*", r"google\w*", r"vyhledej\w*", r"najdi\s+na\b", r"\bsearch\b",
    r"\bbrain\b", r"zapis\w*\s+(?:do|si)", r"zapamatuj\w*", r"uloz\w*", r"\bsave\b", r"pc\b", r"pocitac\w*", r"disk\w*", r"\bproces\w*", r"\bfix\w*", r"oprav\w*",
]
_ACTION_RE = re.compile("|".join(f"(?:{p})" for p in _ACTION))
_PATH_RE = re.compile(r"[A-Za-z]:[\\/]|https?://|\\\\|\b[\w.-]+\.(?:py|js|ts|md|txt|json|toml|ps1|bat|exe|csv|html|log|yaml|yml)\b|`")


def _fold(text):
    """Lowercase and strip diacritics: 'Spusť' -> 'spust'."""
    t = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def compile_triggers(patterns):
    """Compile a list of regex strings (matched against accent-folded, lowercase text) into one pattern.
    Returns None for an empty or missing list, meaning "use the built-in list"."""
    pats = [str(p) for p in (patterns or []) if str(p).strip()]
    if not pats:
        return None
    return re.compile("|".join(f"(?:{p})" for p in pats))


def wants_agent(text, triggers=None):
    """True when the message asks to do something on the computer. `triggers` replaces the built-in word list."""
    t = _fold(text)
    return bool((triggers or _ACTION_RE).search(t) or _PATH_RE.search(text))


def route(text, mode, has_image=False, has_file=False, last_mode=None, agent_triggers=None):
    """Decide how to handle a message. Returns (mode, cleaned_text) with mode in {"agent", "chat"}.
    A leading '!' forces agent and a leading '?' forces chat, whatever the configured mode.
    In auto mode a follow-up ("do it") sticks to the agent when the previous turn was handled by the agent."""
    stripped = text.lstrip()
    if stripped.startswith("!"):
        return "agent", stripped[1:].lstrip()
    if stripped.startswith("?"):
        return "chat", stripped[1:].lstrip()
    if has_file:  # non-image attachment: only the agent can open files
        return "agent", text
    if mode not in MODES:
        mode = "agent"
    if mode == "agent":
        return "agent", text
    if mode == "chat":
        return "chat", text
    return ("agent" if wants_agent(text, agent_triggers) or last_mode == "agent" else "chat"), text


# Built-in (Czech and English) save triggers: explicit "save this" requests and cheap, conservative patterns for lasting facts (accent-folded). Override with save_triggers in the bot config.
_SAVE_EXPLICIT = re.compile(
    r"\buloz\w*\s+(?:si\s+)?(?:to|toto|tohle|ten|tu|do)\b|\bzapamatuj\w*|\bpamatuj\s+si\b|zapis\w*\s+(?:si|do)\b|\bbrain\b"
    r"|\bremember\s+(?:that|this|to)\b|\bsave\s+(?:this|that|it)\b|\bnote\s+(?:this|that)\s+down\b|\bwrite\s+(?:this|that)\s+down\b")
_SAVE_FACT = re.compile(
    r"\bod\s+ted\b|\bvzdy\s+(?:chci|pouzivej|mi)\b|\bnikdy\s+(?:nechci|nepouzivej|mi)\b"
    r"|\bjmenuji\s+se\b|\bmoje\s+(?:jmeno|adresa|telefon|e-?mail|prace|firma|rodina)\b|\b(?:bydlim|pracuji|pracuju)\b"
    r"|\bpreferuji\b|\bmam\s+rad[aiy]?\b|\bnemam\s+rad[aiy]?\b|\bmuj\s+(?:projekt|manzel|partner|syn|bratr)\b|\bmoje\s+(?:zena|dcera|sestra|manzelka)\b"
    r"|\bfrom\s+now\s+on\b|\bmy\s+name\s+is\b|\bi\s+(?:live|work)\s+(?:in|at|for|as)\b|\bi\s+(?:prefer|always|never)\b"
    r"|\bmy\s+(?:wife|husband|daughter|son|address|project|company)\s+(?:is|are)\b")


def wants_save(text, triggers=None):
    """True when a chat-mode message looks like a save request or states a durable fact worth writing to memory.
    Conservative: questions (ending in '?') only count when they hold an explicit save word.
    `triggers` (compiled with compile_triggers) replaces the built-in patterns; any match counts as explicit."""
    t = _fold(text).strip()
    if not t:
        return False
    if triggers:
        return bool(triggers.search(t))
    if _SAVE_EXPLICIT.search(t):
        return True
    return not t.endswith("?") and len(t) >= 12 and bool(_SAVE_FACT.search(t))


def history_prefix(turns, max_chars=6000):
    """Render recent chat-mode turns [(role, text)] as context for an agent session that did not see them."""
    lines = [f"{'User' if r == 'user' else 'You'}: {t}" for r, t in turns]
    body = "\n".join(lines)[-max_chars:]
    if not body:
        return ""
    return ("[Earlier messages in this chat that happened outside this session, for context:\n"
            f"{body}\n]\n\nCurrent message:\n")


def brain_summary(brain_dir, max_chars=BRAIN_MAX_CHARS):
    """Concatenate memory/*.md (README first), truncated to max_chars (README always first)."""
    d = Path(brain_dir)
    if not d.is_dir():
        return ""
    files = sorted(d.glob("*.md"), key=lambda p: (p.name.lower() != "readme.md", p.name.lower()))
    parts, left = [], max_chars
    for f in files:
        if left <= 0:
            break
        try:
            body = f.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        chunk = f"## {f.name}\n{body}"[:left]
        parts.append(chunk)
        left -= len(chunk) + 2
    return "\n\n".join(parts)


def build_system(persona, memory_dir):
    sys_prompt = (persona or "").strip()
    brain = brain_summary(memory_dir) if memory_dir else ""
    if brain:
        sys_prompt += "\n\nBackground notes about the user and their projects (your memory notes, abridged):\n\n" + brain
    return sys_prompt


def image_part(path):
    data = Path(path).read_bytes()
    mime = "image/png" if str(path).lower().endswith(".png") else "image/jpeg"
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(data).decode()}"}}


class ChatSessions:
    """Per-chat message history for chat mode. Cleared by /exit, /new and /cwd."""

    def __init__(self, max_messages=24, max_chars=40000):
        self.history = {}
        self.max_messages, self.max_chars = max_messages, max_chars

    def clear(self, chat):
        return self.history.pop(chat, None) is not None

    def messages(self, chat, system, user_content):
        """Append the user turn and return the full message list for the request."""
        h = self.history.setdefault(chat, [])
        h.append({"role": "user", "content": user_content})
        return [{"role": "system", "content": system}] + h

    def add_reply(self, chat, text, user_text=None):
        h = self.history.setdefault(chat, [])
        if h and isinstance(h[-1]["content"], list):  # do not keep base64 images around in history
            h[-1] = {"role": "user", "content": (user_text or "") + " [image]"}
        h.append({"role": "assistant", "content": text})
        dropped = 0
        while len(h) > self.max_messages or sum(len(str(m["content"])) for m in h) > self.max_chars:
            if len(h) <= 2:
                break
            del h[0]
            dropped += 1
        while h and h[0]["role"] != "user":
            del h[0]
            dropped += 1
        return dropped   # messages forgotten because of the limits (0 = nothing lost)

    def add_turn(self, chat, user_text, reply):
        """Record a finished turn handled by another mode (agent) so chat mode sees it, with the usual trimming."""
        h = self.history.setdefault(chat, [])
        h.append({"role": "user", "content": user_text})
        self.add_reply(chat, reply)

    def drop_last_user(self, chat):
        h = self.history.get(chat)
        if h and h[-1]["role"] == "user":
            h.pop()


def _post(url, body, timeout):
    req = urllib.request.Request(url.rstrip("/") + "/v1/chat/completions", json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


async def complete(url, messages, model, max_tokens=2048, timeout=300, post=_post):
    """One chat completion with thinking disabled (chat_template_kwargs.enable_thinking=false). Returns the reply text."""
    body = {"model": model, "messages": messages, "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": False}}
    d = await asyncio.to_thread(post, url, body, timeout)
    return ((d.get("choices") or [{}])[0].get("message") or {}).get("content", "").strip()
