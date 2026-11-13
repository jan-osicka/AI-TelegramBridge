"""Markdown (as written by the agents) -> Telegram-safe HTML, plus tag-safe splitting.

Telegram HTML supports only a few tags; everything else must be escaped (&, <, >). We split the *Markdown*
into chunks first (fence-aware: a code block cut by a split is closed and reopened) and convert every chunk on
its own, so tags never cross chunk boundaries. If a chunk still renders longer than the limit (escaping can
expand text), it is split again."""
import re

LIMIT = 4096


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _esc_attr(s):
    return esc(s).replace('"', "&quot;")


# ---- inline
_INLINE_CODE = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")
_URL = r"(?:https?://|tg://|mailto:)(?:[^\s()<>]|\([^\s()<>]*\))+"
_LINK = re.compile(r"\[([^\]\n]+)\]\(\s*<?(" + _URL + r")>?\s*\)")
_BOLD = [re.compile(r"(?<![\w*])\*\*(?=\S)(.+?)(?<=\S)\*\*(?![\w*])"),
         re.compile(r"(?<![\w_])__(?=\S)(.+?)(?<=\S)__(?![\w]|\.\w)")]
_ITALIC = [re.compile(r"(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])"),
           re.compile(r"(?<![\w_])_(?=[^\s_])(.+?)(?<=[^\s_])_(?![\w]|\.\w)")]
_STRIKE = re.compile(r"(?<![\w~])~~(?=\S)(.+?)(?<=\S)~~(?![\w~])")


def _emph(escaped):
    for rx in _BOLD:
        escaped = rx.sub(r"<b>\1</b>", escaped)
    for rx in _ITALIC:
        escaped = rx.sub(r"<i>\1</i>", escaped)
    return _STRIKE.sub(r"<s>\1</s>", escaped)


def _spans(s):
    """Links, bold, italic, strike on text that contains no inline code."""
    out, pos = [], 0
    for m in _LINK.finditer(s):
        out.append(_emph(esc(s[pos:m.start()])))
        out.append(f'<a href="{_esc_attr(m.group(2))}">{_emph(esc(m.group(1)))}</a>')
        pos = m.end()
    out.append(_emph(esc(s[pos:])))
    return "".join(out)


def inline(s):
    out, pos = [], 0
    for m in _INLINE_CODE.finditer(s):
        out.append(_spans(s[pos:m.start()]))
        out.append("<code>" + esc(m.group(2)) + "</code>")
        pos = m.end()
    out.append(_spans(s[pos:]))
    return "".join(out)


# ---- lines
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_BULLET = re.compile(r"^(\s*)([-*+])\s+(.*)$")
_HR = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")


def _line(line):
    m = _HEADING.match(line)
    if m:
        body = m.group(1).replace("**", "").replace("__", "")
        return "<b>" + inline(body) + "</b>"
    if _HR.match(line):
        return "――――――"
    m = _BULLET.match(line)
    if m:
        marker = "•" if m.group(2) in "*+" else "-"
        return f"{m.group(1)}{marker} {inline(m.group(3))}"
    return inline(line)


def _render_text(text):
    return "\n".join(_line(ln) for ln in text.split("\n"))


_FENCE = re.compile(r"^[ \t]*```([^\n`]*)\n(.*?)(?:^[ \t]*```[ \t]*$|\Z)", re.M | re.S)
_TAG = re.compile(r"<(/?)(b|i|s|a|code|pre)\b[^>]*>")


def _balanced(html):
    stack = []
    for m in _TAG.finditer(html):
        if m.group(1):
            if not stack or stack.pop() != m.group(2):
                return False
        else:
            stack.append(m.group(2))
    return not stack


def to_html(md):
    """Convert one Markdown chunk. Falls back to escaped plain text if the result would be mis-nested."""
    parts, pos = [], 0
    for m in _FENCE.finditer(md):
        parts.append(_render_text(md[pos:m.start()]))
        lang = m.group(1).strip()
        code = esc(m.group(2).rstrip("\n"))
        cls = f' class="language-{lang}"' if re.fullmatch(r"[\w+#.-]+", lang) else ""
        parts.append(f"<pre><code{cls}>{code}</code></pre>")
        pos = m.end()
    parts.append(_render_text(md[pos:]))
    html = "".join(parts)
    return html if _balanced(html) else esc(md)


# ---- splitting
_FENCE_LINE = re.compile(r"^[ \t]*```")


def split_markdown(text, limit):
    """Split Markdown into chunks of at most `limit` chars on line boundaries. A fenced code block that is
    cut is closed at the end of a chunk and reopened (same language) at the start of the next."""
    if len(text) <= limit:
        return [text]
    cap = max(limit - 40, 16)     # room for the closing/reopening fence lines
    lines = []
    for ln in text.split("\n"):
        while len(ln) > cap:
            cut = ln.rfind(" ", 0, cap)
            if cut < cap // 2:
                lines.append(ln[:cap])
                ln = ln[cap:]
            else:
                lines.append(ln[:cut])
                ln = ln[cut + 1:]
        lines.append(ln)
    chunks, cur, size, fence = [], [], 0, None
    for ln in lines:
        is_fence = bool(_FENCE_LINE.match(ln))
        closing = is_fence and fence is not None
        if cur and size + len(ln) + 1 > cap and not closing:
            chunks.append("\n".join(cur) + ("\n```" if fence else ""))
            cur = [fence] if fence else []
            size = len(fence) + 1 if fence else 0
        cur.append(ln)
        size += len(ln) + 1
        if is_fence:
            fence = None if fence else ln.strip()
    if cur:
        chunks.append("\n".join(cur))
    return [c for c in chunks if c.strip()]


def render_chunks(text, limit=LIMIT):
    """-> [(html, plain)]: every html <= limit chars, tags balanced inside each chunk. `plain` is the Markdown
    source of the same chunk, for the plain-text fallback."""
    out = []

    def go(chunk, lim):
        html = to_html(chunk)
        if len(html) <= limit:
            out.append((html, chunk))
        elif lim < 64:
            out.append((esc(chunk)[:limit], chunk[:limit]))
        else:
            for part in split_markdown(chunk, lim // 2):
                go(part, lim // 2)

    if not (text or "").strip():
        return out
    for part in split_markdown(text, limit * 3 // 4):
        go(part, limit * 3 // 4)
    return out


def with_footer(chunks, footer, limit=LIMIT):
    """Append a plain-text footer to the last chunk (or add it as its own chunk if it would not fit)."""
    if not footer:
        return chunks
    if not chunks:
        return [(esc(footer), footer)]
    html, plain = chunks[-1]
    if len(html) + len(footer) + 2 <= limit and len(plain) + len(footer) + 2 <= limit:
        return chunks[:-1] + [(html + "\n\n" + esc(footer), plain + "\n\n" + footer)]
    return chunks + [(esc(footer), footer)]


def preview_html(text, n=500):
    """Rendered first n chars of a long answer (for the 'full answer attached' message): (html, plain)."""
    cut = text[:n]
    if cut.count("```") % 2:      # do not leave a code fence open
        cut += "\n```"
    return to_html(cut) + "…", cut + "…"
