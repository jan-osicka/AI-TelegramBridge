"""Voice transcription with faster-whisper (CUDA, falling back to CPU int8)."""
import asyncio
import logging
import os
import site
import sys
import threading
from pathlib import Path

log = logging.getLogger("bridge.media")
_model = None
_device = None
_lock = threading.RLock()
_preload_thread = None


def add_cuda_dll_paths():
    if sys.platform != "win32":
        return
    roots = [Path(p) for p in site.getsitepackages()] + [Path(sys.prefix) / "Lib" / "site-packages"]
    for root in roots:
        for sub in ("cublas", "cudnn", "cuda_runtime"):
            d = root / "nvidia" / sub / "bin"
            if d.is_dir():
                os.add_dll_directory(str(d))
                os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")


def _load(model_name, device):
    """Load from the local cache first (no HuggingFace network check); fall back to online if files are missing."""
    from faster_whisper import WhisperModel
    kw = dict(device="cuda", compute_type="float16") if device == "cuda" else dict(device="cpu", compute_type="int8")
    try:
        return WhisperModel(model_name, local_files_only=True, **kw)
    except Exception as e:
        if device == "cuda" and "cuda" in str(e).lower():
            raise  # a CUDA problem, not a missing file: let the caller fall back to CPU
        log.warning("whisper offline load failed (%s); trying online", type(e).__name__)
        return WhisperModel(model_name, **kw)


def get_model(model_name):
    global _model, _device
    with _lock:
        if _model is None:
            add_cuda_dll_paths()
            try:
                _model, _device = _load(model_name, "cuda"), "cuda"
            except Exception as e:
                log.warning("CUDA whisper failed (%s); using CPU int8", e)
                _model, _device = _load(model_name, "cpu"), "cpu"
            log.info("whisper loaded on %s", _device)
        return _model


def preload(model_name, loader=None):
    """Load the model in a background daemon thread so the first voice message does not pay for it.
    Returns the thread immediately; never blocks and never raises."""
    global _preload_thread
    loader = loader or get_model

    def go():
        try:
            loader(model_name)
        except Exception:
            log.exception("whisper preload failed (will retry on first use)")
    _preload_thread = threading.Thread(target=go, name="whisper-preload", daemon=True)
    _preload_thread.start()
    return _preload_thread


def _transcribe(path, model_name):
    global _model, _device

    def go():
        segs, _info = get_model(model_name).transcribe(path, beam_size=5, vad_filter=True)
        return " ".join(s.text.strip() for s in segs).strip()
    try:
        return go()
    except Exception as e:
        if _device == "cuda":
            log.warning("CUDA transcribe failed (%s); retrying on CPU", e)
            with _lock:
                _model, _device = _load(model_name, "cpu"), "cpu"
            return go()
        raise


async def transcribe(path, model_name="large-v3-turbo"):
    return await asyncio.to_thread(_transcribe, str(path), model_name)
