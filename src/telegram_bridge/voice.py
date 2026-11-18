"""Voice replies: the agent ends its answer with a '[tg-voice cs] ... [/tg-voice]' block, the bridge speaks it
with edge-tts (Microsoft neural voices, needs internet), converts it with ffmpeg and sends a Telegram voice message.

extract() and clean_text() are pure; synthesize() is the only part that needs edge-tts and ffmpeg."""
import asyncio
import logging
import re
import shutil
import tempfile
from pathlib import Path

from . import video

log = logging.getLogger("bridge.voice")

MAX_CHARS = 1500
TIMEOUT = 90
NOTE_PREFIX = "🔇 voice reply failed: "
VOICE_MODE_NOTE = "(Voice mode is on: end your reply with a [tg-voice] block.)"
CONVENTION = ("To answer by voice, end your final reply with a block '[tg-voice <lang>]' on its own line, the spoken text, "
              "and '[/tg-voice]' on its own line. <lang> is the language code of your answer (for example {langs}). "
              "The spoken text is a short version meant for listening: plain sentences, no markdown, code, file paths, "
              "URLs or tables. Add the block only when the message says voice mode is on or the user asks for a "
              "spoken answer; otherwise do not use it.")

_OPEN = re.compile(r"^[ \t]*\[tg-voice(?:[ \t]+([A-Za-z-]+))?\][ \t]*(.*)$")
_CLOSE = "[/tg-voice]"


class VoiceError(Exception):
    """The message is a short reason that is safe to show in the chat."""


def convention_sentence(voices) -> str:
    return CONVENTION.replace("{langs}", ", ".join(voices))


def pick_lang(lang, voices, default):
    """Key of `voices` for a marker language ('cs', 'cs-CZ', 'CS'); the default when unknown or missing."""
    l = (lang or "").lower()
    for cand in (l, l.split("-")[0]):
        if cand in voices:
            return cand
    return default


def extract(text: str, voices, default):
    """Return (text without the block, (lang key, spoken text) or None). Markers in code fences are ignored;
    a missing closing tag means the block runs to the end of the text. Only the first block is spoken."""
    kept, spoken, lang = [], None, None
    fence, inblock, buf = None, False, []
    for line in text.splitlines():
        if inblock:
            if _CLOSE in line:
                buf.append(line.split(_CLOSE)[0])
                inblock = False
                if spoken is None:
                    spoken = " ".join(s.strip() for s in buf if s.strip())
            else:
                buf.append(line)
            continue
        fm = re.match(r"[ \t]*(`{3,}|~{3,})", line)
        if fm:
            if fence is None:
                fence = fm.group(1)[0]
            elif fm.group(1)[0] == fence:
                fence = None
        m = None if (fence or fm) else _OPEN.match(line)
        if not m:
            kept.append(line)
            continue
        inblock, buf = True, [m.group(2)]
        if _CLOSE in m.group(2):
            buf, inblock = [m.group(2).split(_CLOSE)[0]], False
            if spoken is None:
                spoken = buf[0].strip()
        lang = lang or m.group(1)
    if inblock and spoken is None:   # no closing tag: until the end of the text
        spoken = " ".join(s.strip() for s in buf if s.strip())
    out = "\n".join(kept).strip()
    spoken = (spoken or "").strip()
    return out, ((pick_lang(lang, voices, default), spoken) if spoken else None)


def clean_text(text: str) -> str:
    """Fallback spoken text from a normal reply: no code, links, URLs or markdown symbols."""
    t = re.sub(r"(?ms)^[ \t]*(`{3,}|~{3,}).*?(?:^[ \t]*\1[^\n]*$|\Z)", " ", text)
    t = re.sub(r"(?m)^[ \t]*\[tg-file\].*$", " ", t)
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"https?://\S+", " ", t)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"(?m)^[ \t]*(?:[#>]+|[-*+]|\d+[.)])[ \t]+", "", t)
    t = re.sub(r"[*_#>|~`]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def cap(text: str, limit: int = MAX_CHARS) -> str:
    """At most `limit` characters, cut at the last sentence end when there is one in the second half."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    ends = [m.end() for m in re.finditer(r"[.!?…](?=\s|$)", cut)]
    if ends and ends[-1] >= limit // 2:
        return cut[:ends[-1]].strip()
    return cut.rsplit(" ", 1)[0].strip() or cut


async def synthesize(text: str, voice: str) -> bytes:
    """OGG/Opus bytes of `text` spoken with the edge-tts `voice`. Raises VoiceError with a short reason."""
    try:
        import edge_tts
    except ImportError:
        raise VoiceError("edge-tts is not installed (uv sync --extra tts)") from None
    if not shutil.which("ffmpeg"):
        raise VoiceError("ffmpeg is not installed")
    tmp = Path(tempfile.mkdtemp(prefix="tb-voice-"))
    try:
        mp3, ogg = tmp / "speech.mp3", tmp / "speech.ogg"
        await asyncio.wait_for(edge_tts.Communicate(text, voice).save(str(mp3)), TIMEOUT)
        r = await asyncio.to_thread(
            video._run, ["ffmpeg", "-v", "error", "-y", "-i", str(mp3), "-c:a", "libopus", "-b:a", "32k", "-ac", "1", str(ogg)])
        if r.returncode != 0 or not ogg.exists() or ogg.stat().st_size == 0:
            raise VoiceError("ffmpeg could not convert the audio")
        return ogg.read_bytes()
    except VoiceError:
        raise
    except asyncio.TimeoutError:
        raise VoiceError("timed out") from None
    except Exception as e:
        log.warning("voice synthesis failed: %s", type(e).__name__)
        raise VoiceError(type(e).__name__) from None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

