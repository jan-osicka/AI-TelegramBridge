"""Mask secrets in text before it leaves the bridge (Telegram messages, logs).

Patterns are adapted from Agent2Telegram's redaction module (MIT licence,
https://github.com/petrludwig-collab/Agent2Telegram), see NOTICE.

Run it on raw text, before Markdown -> HTML conversion and before splitting,
so a mask is never cut in two and the HTML stays valid.

Limits: an unquoted value ends at whitespace (or , ; } ] & and quotes), so a secret with a space
in it is only masked up to the space. Pairs like key=value are masked only when the value looks
like a secret: quoted, or 8+ characters with a digit or a symbol, and not a plain word or type.
"""
import re
from collections.abc import Iterable
from importlib import resources
from urllib.parse import urlsplit, urlunsplit

MASK = "[REDACTED]"
MIN_KNOWN_LEN = 8

# The key word must END the identifier: "api_key", "GITHUB_TOKEN" match, "token_count" does not.
_STRONG = r"[\w-]{0,64}(?:api[_-]?key|token|password|passwd|secret|authorization|credential|private[_-]?key)"
_KEY = r"(?:" + _STRONG + r"|(?:[\w-]{0,64}[_-])?key|pass|heslo)"
_STRONG_RE = re.compile(r"(?i)[\"']?" + _STRONG + r"[\"']?\Z")
_QUOTED = r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*' '''.strip()
_VALUE = "(?:" + _QUOTED + "|(?:" + re.escape(MASK) + r"|[^\s,;}\]&`\"'])+)"
_PAIR = re.compile(r'(?i)(["\']?\b' + _KEY + r'["\']?[ \t]*[:=][ \t]*)' + _VALUE)
_ARG = re.compile(r"(?i)(?<![\w-])(--?" + _KEY + r"\s+)" + _VALUE)
_AUTH = re.compile(r"(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9+/_.=:%-]+")
_TOKENS = re.compile(
    r"(?:\b\d{6,16}:[A-Za-z0-9_-]{20,}"
    r"|\b(?:sk-ant-|sk-proj-|sk-|sk_|ghp_|github_pat_|xox[bp]-|AIza|AKIA)[A-Za-z0-9_-]{6,})")
_URL = re.compile(r"""https?://[^\s<>"'`]+""", re.I)
_SENSITIVE_PARAM = re.compile(
    r"(?i)(?:^|[_-])(?:key|token|secret|password|passwd|sig|signature|auth|code|apikey|api[_-]?key|credential)s?$")
_PLAIN_WORDS = {"str", "int", "float", "bool", "bytes", "list", "dict", "set", "tuple", "none", "null", "true",
                "false", "ok", "len", "missing", "unset", "empty", "required", "invalid", "value", "string", "number", "object", "any", "undefined"}


def _secret_like(value):
    """Is an unquoted value in a key=value pair plausibly a secret, not code or prose?"""
    if value == MASK or value[:1] in "\"'":
        return True
    if len(value) < 8 or value.lower() in _PLAIN_WORDS:
        return False
    return any(c.isdigit() for c in value) or not re.fullmatch(r"\w+", value)


def _strong_secret(value):
    """Value after a strong key (password, token...): mask unless it is a plain word/type or looks like code."""
    return value == MASK or value[:1] in "\"'" or not (value.lower() in _PLAIN_WORDS or "(" in value or ")" in value or value[:1] == "<")


def _sensitive_params(s):
    for part in re.split(r"[&;]", s):
        name, eq, val = part.partition("=")
        if eq and (_SENSITIVE_PARAM.search(name) or _TOKENS.search(val)):
            return True
    return False

# Seed phrases are plain words, so no pattern above sees them: mask 12+ BIP39 words in a row.
# shortcut: English wordlist only, add other BIP39 lists (cs, es...) if someone needs them.
_BIP39 = frozenset((resources.files("telegram_bridge") / "bip39_english.txt").read_text("ascii").split())
SEED_MIN_WORDS = 12
_WORD = re.compile(r"[A-Za-z]+")
# Between seed words only spaces, commas, quotes, brackets and "1." / "2)" numbering. Not - _ . / so
# code identifiers (field_number, top-left, border-image-slice) never form a run.
_SEED_GAP = re.compile(r"""(?:[\s,"'\[\]]|\d{1,2}[.):]?)+\Z""")


def _close(run, runs):
    # Seeds rarely repeat a word; code and UI text do (false,false,false / Please ... this job).
    if len(run) >= SEED_MIN_WORDS and len({m.group().lower() for m in run}) * 4 >= len(run) * 3:
        runs.append((run[0].start(), run[-1].end()))


def _mask_seeds(text):
    """Replace each run of SEED_MIN_WORDS+ consecutive BIP39 words. Returns (text, count)."""
    runs, run = [], []
    for m in _WORD.finditer(text):
        # Gap capped at 10 chars before the regex: keeps it fast (no backtracking on long digit runs).
        if m.group().lower() in _BIP39 and (not run or (m.start() - run[-1].end() <= 10
                                                        and _SEED_GAP.match(text, run[-1].end(), m.start()))):
            run.append(m)
            continue
        _close(run, runs)
        run = [m] if m.group().lower() in _BIP39 else []
    _close(run, runs)
    for a, b in reversed(runs):
        text = text[:a] + MASK + text[b:]
    return text, len(runs)


_known: list[str] = []


def set_known(values: Iterable[str]) -> None:
    """Register exact secret values (longest first). Values shorter than 8 chars are ignored."""
    global _known
    _known = sorted({v for v in values if v and len(v) >= MIN_KNOWN_LEN}, key=len, reverse=True)


def secret_values(env: dict) -> list[str]:
    """The .env values that count as secrets (keys ending in TOKEN/KEY/SECRET/PASSWORD, and TOTP_SECRET)."""
    return [v for k, v in env.items() if k.endswith(("TOKEN", "KEY", "SECRET", "PASSWORD")) or k == "TOTP_SECRET"]


def redact(text: str, known: Iterable[str] = ()) -> tuple[str, int]:
    """Return (masked text, number of masks). `known` adds to the registry for this call."""
    n = 0

    def pair(m):
        key = re.sub(r"[ \t]*[:=][ \t]*\Z", "", m.group(1)).strip(" \t\r\n-")
        like = _strong_secret if _STRONG_RE.match(key) else _secret_like
        if not like(m.group()[len(m.group(1)):]):
            return m.group()
        return hit(m)

    def hit(m, keep=1):
        nonlocal n
        if keep and m.group().endswith(MASK) and m.group()[len(m.group(1)):] == MASK:
            return m.group()   # already masked
        n += 1
        return (m.group(1) if keep else "") + MASK

    def url(m):
        nonlocal n
        raw = m.group()
        try:
            u = urlsplit(raw)
        except ValueError:
            return raw
        mq = bool(u.query) and _sensitive_params(u.query)
        mf = bool(u.fragment) and _sensitive_params(u.fragment)
        if not (mq or mf):
            return raw
        n += 1
        return urlunsplit((u.scheme, u.netloc, u.path, MASK if mq else u.query, MASK if mf else u.fragment))

    vals = sorted({v for v in (*_known, *known) if v and len(v) >= MIN_KNOWN_LEN}, key=len, reverse=True)
    for v in vals:
        c = text.count(v)
        if c:
            text, n = text.replace(v, MASK), n + c
    text, seeds = _mask_seeds(text)
    n += seeds
    text = _URL.sub(url, text)
    text = _AUTH.sub(lambda m: hit(m, 0), text)
    text = _PAIR.sub(pair, text)
    text = _ARG.sub(pair, text)
    text = _TOKENS.sub(lambda m: hit(m, 0), text)
    return text, n
