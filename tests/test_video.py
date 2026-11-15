import asyncio
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_bridge import media, video
from test_frontend import make


def test_frame_times_even_spacing():
    assert video.frame_times(8.0, 4) == [1.0, 3.0, 5.0, 7.0]
    assert video.frame_times(None, 3) == [0.0, 1.0, 2.0]


def test_frame_count_rules():
    assert video.frame_count(30, 4) == 4
    assert video.frame_count(61, 4) == 8
    assert video.frame_count(120, 6) == 8
    assert video.frame_count(30, 0) == 0
    assert video.frame_count(None, 4) == 4


def test_probe_none_when_missing(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError
    monkeypatch.setattr(video.subprocess, "run", boom)
    assert video.probe("x.mp4") is None
    assert video.extract_frames("x.mp4", Path("."), 0) == []


def test_extract_frames_never_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(video.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    assert video.extract_frames(tmp_path / "v.mp4", tmp_path, 3) == []
    assert video.extract_audio(tmp_path / "v.mp4", tmp_path) is None


def test_extract_audio_none_on_ffmpeg_error(monkeypatch, tmp_path):
    monkeypatch.setattr(video.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=1, stdout=b"", stderr=b"no audio"))
    assert video.extract_audio(tmp_path / "v.mp4", tmp_path) is None


class Status:
    def __init__(self):
        self.deleted = False

    async def delete(self):
        self.deleted = True


class Msg:
    text = None
    voice = audio = photo = document = animation = video_note = None
    chat_id, message_id = 5, 1

    def __init__(self, size=1000, caption=None):
        self.video = SimpleNamespace(file_id="v", file_size=size, file_name="clip.mp4")
        self.caption = caption
        self.replies, self.status = [], Status()

    async def reply_text(self, t, **kw):
        self.replies.append(t)
        return self.status


def setup(tmp_path, monkeypatch, **general):
    s = make(**general)
    inbox = tmp_path / "inbox"
    inbox.mkdir(exist_ok=True)

    async def dl(ctx, fid, name):
        p = inbox / f"clip-{name}"
        p.write_bytes(b"x")
        return p
    s.download = dl
    return s


def build(s, m):
    upd = SimpleNamespace(effective_message=m, effective_chat=SimpleNamespace(id=5))
    return asyncio.run(s.build_prompt(upd, None, m.caption or ""))


def patch_video(monkeypatch, dur=42.0, audio=True, ffmpeg=True, transcript="hello there"):
    monkeypatch.setattr(video, "have_ffmpeg", lambda: ffmpeg)
    monkeypatch.setattr(video, "probe", lambda p: dur)
    asked = {}

    def frames(p, out, n, dur=None):
        asked["n"] = n
        return [Path(out) / f"{Path(p).stem}-frame{i}.jpg" for i in range(1, n + 1)]
    monkeypatch.setattr(video, "extract_frames", frames)

    def aud(p, out):
        if not audio:
            return None
        w = Path(out) / "a.wav"
        w.write_bytes(b"w")
        return w
    monkeypatch.setattr(video, "extract_audio", aud)

    async def tr(path, model="x"):
        asked["tr"] = True
        return transcript
    monkeypatch.setattr(media, "transcribe", tr)
    return asked


def test_too_big(tmp_path, monkeypatch):
    s = setup(tmp_path, monkeypatch)
    m = Msg(size=21 * 1024 * 1024)
    assert build(s, m) is None
    assert m.replies == ["🎬 Video is too big for Telegram bots (max 20 MB). Send a shorter clip."]


def test_no_caption_prompt_and_format(tmp_path, monkeypatch):
    s = setup(tmp_path, monkeypatch)
    asked = patch_video(monkeypatch)
    m = Msg()
    item = build(s, m)
    assert asked["n"] == 4
    p = item["prompt"]
    assert p.startswith("Look at this video\n\nVideo file: ")
    assert "\nTranscript of the audio: hello there" in p
    assert "\nStill frames (in order): " in p and p.count("-frame") == 4
    assert len(item["images"]) == 4 and item["mode"] == "agent"
    assert m.status.deleted and m.replies[0] == "🎬 Processing video…"
    assert m.replies[-1].startswith("🎬 0:42 · 4 frames") and "🎙️ hello there" in m.replies[-1]
    assert not list((tmp_path / "inbox").glob("*.wav"))


def test_download_too_big_error_friendly(tmp_path, monkeypatch):
    s = setup(tmp_path, monkeypatch)

    async def dl(ctx, fid, name):
        raise Exception("File is too big")
    s.download = dl
    m = Msg(size=None)
    assert build(s, m) is None
    assert m.replies == ["🎬 Video is too big for Telegram bots (max 20 MB). Send a shorter clip."]


def test_summary_reply_failure_keeps_video(tmp_path, monkeypatch):
    s = setup(tmp_path, monkeypatch)
    patch_video(monkeypatch)

    async def bad_say(msg, text, **kw):
        raise RuntimeError("boom")
    s.say = bad_say
    item = build(s, Msg())
    assert item and len(item["images"]) == 4


def test_no_voice_extra_note(tmp_path, monkeypatch):
    s = setup(tmp_path, monkeypatch)
    patch_video(monkeypatch)

    async def tr(path, model="x"):
        raise ImportError("faster_whisper")
    monkeypatch.setattr(media, "transcribe", tr)
    m = Msg()
    item = build(s, m)
    assert item and "Transcript" not in item["prompt"]


def test_duration_passed_to_frames(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(video, "probe", lambda p: calls.append(1) or 10.0)
    monkeypatch.setattr(video, "_run", lambda c: SimpleNamespace(returncode=1))
    video.extract_frames(tmp_path / "a.mp4", tmp_path, 2, 10.0)
    assert not calls


def test_caption_used(tmp_path, monkeypatch):
    s = setup(tmp_path, monkeypatch)
    patch_video(monkeypatch)
    assert build(s, Msg(caption="what is this"))["prompt"].startswith("what is this\n\nVideo file: ")


def test_long_video_doubles_and_zero_disables(tmp_path, monkeypatch):
    s = setup(tmp_path, monkeypatch)
    asked = patch_video(monkeypatch, dur=90)
    build(s, Msg())
    assert asked["n"] == 8
    s = setup(tmp_path, monkeypatch, video_frames=0)
    asked = patch_video(monkeypatch)
    item = build(s, Msg())
    assert "n" not in asked and item["images"] == [] and "Still frames" not in item["prompt"]


def test_no_audio_no_transcript(tmp_path, monkeypatch):
    s = setup(tmp_path, monkeypatch)
    asked = patch_video(monkeypatch, audio=False)
    item = build(s, Msg())
    assert "tr" not in asked and "Transcript" not in item["prompt"]


def test_transcribe_off(tmp_path, monkeypatch):
    s = setup(tmp_path, monkeypatch, video_transcribe=False)
    asked = patch_video(monkeypatch)
    assert "Transcript" not in build(s, Msg())["prompt"] and "tr" not in asked


def test_missing_ffmpeg_note(tmp_path, monkeypatch):
    s = setup(tmp_path, monkeypatch)
    patch_video(monkeypatch, ffmpeg=False)
    m = Msg()
    item = build(s, m)
    assert item["prompt"].startswith("Look at this video\n\nVideo file: ")
    assert item["images"] == []
    assert "ffmpeg not found: only the file is passed on" in m.replies[-1]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_real_clip(tmp_path):
    clip = tmp_path / "t.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=3:size=640x360:rate=10",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-shortest", str(clip)], check=True)
    assert 2.5 < video.probe(clip) < 3.5
    frames = video.extract_frames(clip, tmp_path, 4)
    assert len(frames) == 4 and all(f.stat().st_size > 0 for f in frames)
    wav = video.extract_audio(clip, tmp_path)
    assert wav and wav.suffix == ".wav"
    silent = tmp_path / "s.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=64x64:rate=5",
                    str(silent)], check=True)
    assert video.extract_audio(silent, tmp_path) is None
