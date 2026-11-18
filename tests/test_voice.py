"""Voice replies: marker extraction, /voice mode, sending order, failures, chat mode, config. No network."""
import asyncio
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_bridge import bridge, settings
from telegram_bridge import chat as chatmod
from telegram_bridge import voice as voicemod
from telegram_bridge.auth import Auth
from telegram_bridge.bridge import BotService
from test_frontend import SECRET, Clock, FakeBot, script_run

VOICES = {"cs": "cs-VOICE", "en": "en-VOICE"}
VCFG = {"voices": VOICES, "default_lang": "en"}


class VBot(FakeBot):
    def __init__(self):
        super().__init__()
        self.events = []

    async def send_message(self, chat, text, **kw):
        self.events.append(("text", text))
        return await super().send_message(chat, text, **kw)

    async def send_voice(self, chat, f, **kw):
        self.events.append(("voice", f.read()))


def make(name="claude", voice=VCFG, auth=None):
    cfg = settings.load_config()
    bot_cfg = {**cfg["bots"][name], "voice": voice}
    s = BotService(name, bot_cfg, "t", auth or Auth([5], SECRET), cfg["general"], clock=Clock(0.0))
    s.app = SimpleNamespace(bot=VBot())
    return s


@pytest.fixture
def synth(monkeypatch):
    calls = []

    async def fake(text, voice):
        calls.append((text, voice))
        return b"OGG"
    monkeypatch.setattr(voicemod, "synthesize", fake)
    return calls


# ---- extraction
def test_extract_normal_and_language():
    text = "Full answer.\n[tg-voice cs]\nKrátká verze.\nDruhá věta.\n[/tg-voice]"
    assert voicemod.extract(text, VOICES, "en") == ("Full answer.", ("cs", "Krátká verze. Druhá věta."))
    assert voicemod.extract("a\n[tg-voice cs-CZ]x[/tg-voice]\nb", VOICES, "en") == ("a\nb", ("cs", "x"))


def test_extract_unknown_or_missing_language_uses_default():
    assert voicemod.extract("a\n[tg-voice de]\nhallo\n[/tg-voice]", VOICES, "en")[1] == ("en", "hallo")
    assert voicemod.extract("a\n[tg-voice]\nhi\n[/tg-voice]", VOICES, "en")[1] == ("en", "hi")


def test_extract_missing_close_tag_runs_to_end():
    out, sp = voicemod.extract("Answer\n[tg-voice en]\nspoken one\nspoken two", VOICES, "en")
    assert out == "Answer" and sp == ("en", "spoken one spoken two")


def test_extract_ignores_fenced_marker():
    text = "Example:\n```\n[tg-voice en]\nnot spoken\n[/tg-voice]\n```\nend"
    assert voicemod.extract(text, VOICES, "en") == (text, None)


def test_clean_text_and_cap():
    raw = "# Title\n**Bold** and `code` see [docs](http://x.io/a) http://y.io\n```\nrm -rf\n```\n- item\n[tg-file] /a/b"
    assert voicemod.clean_text(raw) == "Title Bold and code see docs item"
    out = voicemod.cap("Sentence one. " * 200)
    assert len(out) <= voicemod.MAX_CHARS and out.endswith("one.")
    assert voicemod.cap("short") == "short"


# ---- /voice command and reset
def cmd(s, arg, name="voice"):
    sent = []

    async def reply_text(t, **kw):
        sent.append(t)
    asyncio.run(s.command(SimpleNamespace(effective_message=SimpleNamespace(reply_text=reply_text)), name, arg, 5, 5))
    return sent[0]


def test_voice_toggle_state_and_help():
    s = make()
    assert "is off" in cmd(s, "")
    assert "is on" in cmd(s, "on") and 5 in s.voice_on
    assert "is on" in cmd(s, "")
    assert "is off" in cmd(s, "off") and 5 not in s.voice_on
    assert "Usage" in cmd(s, "maybe")
    assert any(c == "voice" for c, _ in bridge.commands_for(s.cfg)) and "/voice" in s.help()


def test_voice_not_configured():
    s = make(voice=None)
    assert "not configured" in cmd(s, "on") and not s.voice_on
    assert not any(c == "voice" for c, _ in bridge.commands_for(s.cfg))
    assert "[tg-voice" not in (s.cfg.get("append_system_prompt") or "")


def test_voice_mode_reset_on_every_kind_of_lock():
    clock = Clock()
    auth = Auth([5], SECRET, session_hours=1, idle_lock_hours=0.5, clock=clock)
    s = make(auth=auth)
    for how in ("lock", "idle", "expiry"):
        auth._expiry[5] = clock.t + (3600 if how != "expiry" else 10)
        auth._activity[5] = clock.t
        s.voice_on.add(5)
        if how == "lock":
            auth.lock(5)
        else:
            clock.t += 2000 if how == "idle" else 20
            assert not auth.is_unlocked(5)
        assert not s.voice_on, how


def test_system_prompt_sentence_with_voice_table():
    s = make()
    prompt = s.cfg["append_system_prompt"]
    assert prompt.count("[tg-voice") >= 1 and "cs, en" in prompt
    assert prompt.count("[tg-file]") == 1


# ---- sending
def test_voice_sent_before_text_and_block_removed(synth):
    s = make()
    script_run(s, "Full text.\n[tg-voice cs]\nAhoj světe.\n[/tg-voice]")
    asyncio.run(s.run_task(5, "hi", 77))
    assert [k for k, _ in s.app.bot.events] == ["text", "voice", "text"]   # working status, voice, answer
    assert synth == [("Ahoj světe.", "cs-VOICE")]
    assert s.app.bot.events[-1][1] == "Full text."


def test_voice_mode_prompt_note_and_fallback_without_block(synth):
    s = make()
    s.voice_on.add(5)
    seen = []

    async def run(prompt, session_id=None, on_event=None):
        seen.append(prompt)
        return "Plain **answer** with `code`.", "sid", False
    s.runner.run = run
    asyncio.run(s.run_one(5, {"mode": "agent", "prompt": "hello", "text": "hello", "images": []}))
    assert seen[0].startswith("hello") and "Voice mode is on" in seen[0]
    assert synth == [("Plain answer with code.", "en-VOICE")]


def test_voice_off_means_no_voice_without_block(synth):
    s = make()
    script_run(s, "Just text")
    asyncio.run(s.run_task(5, "hi", 77))
    assert synth == [] and [k for k, _ in s.app.bot.events] == ["text", "text"]


def test_spoken_text_is_redacted_and_capped(synth):
    s = make()
    script_run(s, "ok\n[tg-voice en]\nthe key is token=abcdef123456XYZ. " + "More words here. " * 200 + "\n[/tg-voice]")
    asyncio.run(s.run_task(5, "hi", 77))
    assert "abcdef123456XYZ" not in synth[0][0] and len(synth[0][0]) <= voicemod.MAX_CHARS


def test_synthesis_failure_still_sends_text_with_note(monkeypatch):
    async def boom(text, voice):
        raise voicemod.VoiceError("ffmpeg is not installed")
    monkeypatch.setattr(voicemod, "synthesize", boom)
    s = make()
    script_run(s, "Full text.\n[tg-voice en]\nspoken\n[/tg-voice]")
    asyncio.run(s.run_task(5, "hi", 77))
    assert [k for k, _ in s.app.bot.events] == ["text", "text"]
    assert s.app.bot.events[-1][1] == "Full text.\n\n🔇 voice reply failed: ffmpeg is not installed"


def test_send_voice_failure_never_raises(synth):
    s = make()

    async def bad(chat, f, **kw):
        raise ConnectionError("down")
    s.app.bot.send_voice = bad
    assert asyncio.run(s.send_voice(5, ("en", "hi"))) == "🔇 voice reply failed: ConnectionError"


def test_synthesize_reasons_and_cleanup(monkeypatch):
    monkeypatch.setitem(sys.modules, "edge_tts", None)   # import fails
    with pytest.raises(voicemod.VoiceError, match="not installed"):
        asyncio.run(voicemod.synthesize("x", "v"))
    saved = []

    class Comm:
        def __init__(self, text, voice):
            pass

        async def save(self, path):
            saved.append(path)
            Path(path).write_bytes(b"mp3")
    monkeypatch.setitem(sys.modules, "edge_tts", SimpleNamespace(Communicate=Comm))
    monkeypatch.setattr(shutil, "which", lambda n: None)
    with pytest.raises(voicemod.VoiceError, match="ffmpeg is not installed"):
        asyncio.run(voicemod.synthesize("x", "v"))
    monkeypatch.setattr(shutil, "which", lambda n: "ffmpeg")

    def fake_run(cmd):
        Path(cmd[-1]).write_bytes(b"ogg-data")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(voicemod.video, "_run", fake_run)
    assert asyncio.run(voicemod.synthesize("x", "v")) == b"ogg-data"
    assert not Path(saved[0]).parent.exists()   # temp folder removed


# ---- chat mode (local-model bot)
def test_chat_mode_gets_sentence_note_and_voice(monkeypatch, synth):
    s = make("qwen")
    s.voice_on.add(5)
    seen = []

    async def fake_complete(url, messages, **kw):
        seen.append(messages)
        return "Hi there.\n[tg-voice en]\nHello.\n[/tg-voice]"
    monkeypatch.setattr(chatmod, "complete", fake_complete)
    asyncio.run(s.run_chat(5, {"mode": "chat", "text": "ahoj", "prompt": "ahoj", "images": []}))
    assert "[tg-voice" in seen[0][0]["content"]
    assert "Voice mode is on" in seen[0][-1]["content"]
    assert s.chats.history[5][0]["content"] == "ahoj"   # the note is not saved in history
    assert s.chats.history[5][1]["content"] == "Hi there."
    assert synth == [("Hello.", "en-VOICE")]
    assert [k for k, _ in s.app.bot.events] == ["voice", "text"]


# ---- config
def test_config_normalised(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[general]\n[bots.a]\n[bots.a.voice]\nvoices = { CS = "x", en = "y", bad = 3 }\ndefault_lang = "de"\n'
                 '[bots.b]\n[bots.c.voice]\nvoices = {}\n', encoding="utf-8")
    bots = settings.load_config(p)["bots"]
    assert bots["a"]["voice"] == {"voices": {"cs": "x", "en": "y"}, "default_lang": "cs"}
    assert len(bots["a"]["voice_problems"]) == 2
    assert bots["b"]["voice"] is None and bots["b"]["voice_problems"] == []
    assert bots["c"]["voice"] is None and bots["c"]["voice_problems"]


def test_example_config_voices():
    import tomllib
    from importlib import resources
    text = resources.files("telegram_bridge").joinpath("config.example.toml").read_text(encoding="utf-8")
    b = tomllib.loads(text)["bots"]["claude"]["voice"]
    assert set(b["voices"]) == {"cs", "sk", "en"} and b["default_lang"] == "en"
