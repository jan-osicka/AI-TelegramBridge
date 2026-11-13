"""One-time setup: home folder, .env template, TOTP secret + QR, permissions, example config.

The QR code (totp-qr.png) encodes the TOTP secret: scan it, then delete the file.
Safe to re-run; existing values and an existing config.toml are never overwritten.
"""
import re
from importlib import resources
import sys

import pyotp

from . import osutil, settings

TEMPLATE = "CLAUDE_BOT_TOKEN=\nQWEN_BOT_TOKEN=\nALLOWED_USER_IDS=\nTOTP_SECRET=\n"
WARNING = "This bot can run any command on this computer. The whitelist, 2FA and the safety rules in the prompt are what protect it."
QR_NOTE = "scan it, then DELETE the file: it encodes the TOTP secret"


def download_whisper(model_name: str) -> int:
    """Download the Whisper model into the default HuggingFace cache and test CUDA."""
    try:
        from . import media
        media.add_cuda_dll_paths()
        from faster_whisper import WhisperModel
    except ImportError:
        print("faster-whisper is not installed. Install the voice extra: uv sync --extra voice", file=sys.stderr)
        return 1
    WhisperModel(model_name, device="cpu", compute_type="int8")
    print(f"Whisper model {model_name} downloaded")
    try:
        WhisperModel(model_name, device="cuda", compute_type="float16")
        print("CUDA load OK")
    except Exception as e:
        print(f"CUDA not usable, voice will run on CPU ({type(e).__name__})")
    return 0


def _whisper_model(cfg_path) -> str:
    try:
        return settings.load_config(cfg_path)["general"].get("whisper_model", "large-v3-turbo")
    except Exception:
        return "large-v3-turbo"


def run(args) -> int:
    home = settings.home()
    home.mkdir(parents=True, exist_ok=True)
    for sub in ("inbox", "outbox", "logs"):
        (home / sub).mkdir(exist_ok=True)
    env = home / ".env"
    if not env.exists():
        env.write_text(TEMPLATE, encoding="utf-8")
        print("Created", env)
    text = env.read_text(encoding="utf-8")
    m = re.search(r"^TOTP_SECRET=(.*)$", text, re.M)
    secret = m.group(1).strip() if m else ""
    if not secret:
        secret = pyotp.random_base32()
        if m:
            text = text[:m.start()] + f"TOTP_SECRET={secret}" + text[m.end():]
        else:
            text = text.rstrip("\n") + f"\nTOTP_SECRET={secret}\n"
        env.write_text(text, encoding="utf-8")
        print("Generated TOTP_SECRET")
    uri = pyotp.TOTP(secret).provisioning_uri(name="telegram-bridge", issuer_name="Telegram Bridge")
    try:
        import qrcode
        png = home / "totp-qr.png"
        qrcode.make(uri).save(png)
        print(f"QR code: {png} ({QR_NOTE})")
    except ImportError:
        print("The qr extra is not installed (uv sync --extra qr), so no QR image was made.")
        print(f"Add this to your authenticator manually, then clear your terminal (and DELETE any copy): it contains the TOTP secret.\n{uri}")
    cfg = home / "config.toml"
    if not cfg.exists():
        example = resources.files("telegram_bridge") / "config.example.toml"
        if example.is_file():
            cfg.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
            print("Created", cfg)
        else:
            print(f"config.example.toml not found in the package; create {cfg} by hand.", file=sys.stderr)
    ok, msg = osutil.restrict_permissions(home)
    print("Permissions restricted to your user" if ok else f"Could not restrict permissions: {msg}")
    print("\nWARNING:", WARNING)
    print(f"\nNext: put your bot token(s) and ALLOWED_USER_IDS into {env}, then run: telegram-bridge doctor")
    if getattr(args, "whisper", False):
        return download_whisper(_whisper_model(cfg if cfg.exists() else None))
    return 0
