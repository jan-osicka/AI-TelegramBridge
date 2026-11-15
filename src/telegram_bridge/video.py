"""Video helpers: probe, still frames and audio extraction with ffmpeg. Nothing here raises."""
import logging
import shutil
import subprocess
from pathlib import Path

from . import osutil

log = logging.getLogger("bridge.video")
TIMEOUT = 60
MAX_SIDE = 1280
LONG_VIDEO_S = 60
MAX_FRAMES = 8


def have_ffmpeg():
    return bool(shutil.which("ffmpeg"))


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, timeout=TIMEOUT, **osutil.spawn_kwargs())


def probe(path):
    """Duration in seconds, or None when ffprobe is missing or fails."""
    try:
        r = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                  "-of", "default=nw=1:nk=1", str(path)])
        return float(r.stdout.decode().strip()) if r.returncode == 0 else None
    except Exception as e:
        log.warning("ffprobe failed: %s", type(e).__name__)
        return None


def frame_count(duration, configured):
    """Number of frames: configured, doubled for videos over 60 s, at most 8; 0 disables."""
    n = int(configured or 0)
    if n <= 0:
        return 0
    if duration and duration > LONG_VIDEO_S:
        n *= 2
    return min(n, MAX_FRAMES)


def frame_times(duration, count):
    if duration and duration > 0:
        return [duration * (i + 0.5) / count for i in range(count)]
    return [float(i) for i in range(count)]


def extract_frames(path, out_dir, count, duration=None):
    path, out_dir = Path(path), Path(out_dir)
    out = []
    if count <= 0:
        return out
    out_dir.mkdir(parents=True, exist_ok=True)
    for i, t in enumerate(frame_times(duration if duration is not None else probe(path), count), 1):
        dest = out_dir / f"{path.stem}-frame{i}.jpg"
        try:
            r = _run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1",
                      "-vf", f"scale='min({MAX_SIDE},iw)':'min({MAX_SIDE},ih)':force_original_aspect_ratio=decrease",
                      "-q:v", "3", str(dest)])
            if r.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
                out.append(dest)
        except Exception as e:
            log.warning("ffmpeg frame %d failed: %s", i, type(e).__name__)
            if isinstance(e, FileNotFoundError):
                break
    return out


def extract_audio(path, out_dir):
    """16 kHz mono wav next to the frames, or None (no audio stream, ffmpeg missing or failed)."""
    path, out_dir = Path(path), Path(out_dir)
    dest = out_dir / f"{path.stem}-audio.wav"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        r = _run(["ffmpeg", "-v", "error", "-y", "-i", str(path), "-vn", "-map", "0:a:0",
                  "-ac", "1", "-ar", "16000", str(dest)])
        if r.returncode == 0 and dest.exists() and dest.stat().st_size > 44:
            return dest
    except Exception as e:
        log.warning("ffmpeg audio failed: %s", type(e).__name__)
    dest.unlink(missing_ok=True)
    return None


def fmt_duration(sec):
    s = int(round(sec or 0))
    return f"{s // 60}:{s % 60:02d}"
