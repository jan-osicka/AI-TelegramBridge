import logging
import os
import tomllib
from pathlib import Path

from dotenv import dotenv_values

log = logging.getLogger(__name__)


def _code_dir() -> Path:
    pkg = Path(__file__).resolve().parent
    root = pkg.parent.parent
    # Source checkout (src/telegram_bridge next to pyproject.toml): use the repo root.
    if pkg.parent.name == "src" and (root / "pyproject.toml").exists():
        return root
    return pkg


def home() -> Path:
    env = os.environ.get("TELEGRAM_BRIDGE_HOME")
    return Path(env) if env else Path.home() / ".telegram-bridge"


CODE_DIR = _code_dir()
STATE_DIR = home()
INBOX = STATE_DIR / "inbox"
OUTBOX = STATE_DIR / "outbox"
LOGS = STATE_DIR / "logs"
LOCK = STATE_DIR / "bridge.lock"


def config_path() -> Path:
    p = STATE_DIR / "config.toml"
    if p.exists():
        return p
    legacy = CODE_DIR / "config.toml"
    log.warning("config.toml not found in %s; falling back to %s", STATE_DIR, legacy)
    return legacy


def _expand(value, tokens):
    if isinstance(value, str):
        for k, v in tokens.items():
            value = value.replace(k, v)
        return value
    if isinstance(value, list):
        return [_expand(v, tokens) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v, tokens) for k, v in value.items()}
    return value


DEFAULT_MEMORY_NOTICE = "💾 Saving this to memory (agent run follows)."


def _normalise_memory(bot):
    """[bots.X.memory]: optional notes folder for chat mode. Off unless enabled = true."""
    mem = dict(bot.get("memory") or {})
    mem["enabled"] = bool(mem.get("enabled", False))
    d = str(mem.get("dir") or "").strip()
    mem["dir"] = str(Path(d).expanduser()) if d else ""
    mem["save_prompt"] = str(mem.get("save_prompt") or "")
    mem["notice_text"] = str(mem.get("notice_text") or DEFAULT_MEMORY_NOTICE)
    bot["memory"] = mem
    for key in ("agent_triggers", "save_triggers"):
        bot[key] = [str(x) for x in (bot.get(key) or [])]


def _normalise_voice(bot):
    """[bots.X.voice]: edge-tts voices per language. bot["voice"] is None (off) or {voices, default_lang};
    bot["voice_problems"] lists what was wrong (shown by doctor)."""
    raw = bot.get("voice")
    bot["voice"], bot["voice_problems"] = None, []
    if raw is None:
        return
    problems = []
    if not isinstance(raw, dict):
        raw, problems = {}, ["[voice] is not a table"]
    voices = raw.get("voices")
    if not isinstance(voices, dict):
        voices = {}
        problems.append("voices must be a table such as { en = \"en-US-AndrewNeural\" }")
    good = {str(k).strip().lower(): v.strip() for k, v in voices.items() if isinstance(v, str) and v.strip()}
    if len(good) != len(voices):
        problems.append("some voices are not non-empty strings")
    default = str(raw.get("default_lang") or "en").strip().lower()
    if good and default not in good:
        problems.append(f"default_lang {default!r} has no voice; using {next(iter(good))!r}")
        default = next(iter(good))
    if not good:
        problems.append("no usable voices; voice replies are off")
    bot["voice"] = {"voices": good, "default_lang": default} if good else None
    bot["voice_problems"] = problems


def _normalise(cfg):
    g = cfg.setdefault("general", {})
    lm = dict(cfg.get("local_model") or cfg.get("qwen_server") or {})
    lm.setdefault("url", g.get("strata_url"))
    lm.setdefault("idle_stop_minutes", g.get("qwen_idle_stop_minutes", 20))
    lm.setdefault("startup_timeout_minutes", g.get("qwen_startup_timeout_minutes", 6))
    if not lm.get("busy_file"):
        lm["busy_file"] = g.get("qwen_busy_file") or None
    if lm["busy_file"]:
        lm["busy_file"] = str(Path(lm["busy_file"]).expanduser())
    lm.setdefault("start_command", None)
    lm.setdefault("stop_command", None)
    cfg["local_model"] = lm
    for key, bot in (cfg.get("bots") or {}).items():
        bot["needs_local_model"] = bool(bot.get("needs_local_model", bot.get("needs_strata", False)))
        bot["display_name"] = bot.get("display_name") or key.capitalize()
        bot["mode"] = bot.get("mode") or "agent"
        _normalise_memory(bot)
        _normalise_voice(bot)
        bot["chat_model"] = bot.get("chat_model") or (bot.get("env") or {}).get("ANTHROPIC_MODEL") or "local"
    return cfg


def load_config(path: Path | None = None) -> dict:
    with open(path or config_path(), "rb") as f:
        cfg = tomllib.load(f)
    tokens = {"{home}": str(home()), "{code_dir}": str(CODE_DIR)}
    cwd = _expand((cfg.get("general") or {}).get("cwd"), tokens)
    if cwd:
        tokens["{cwd}"] = cwd
    return _normalise(_expand(cfg, tokens))


def load_secrets():
    vals = dotenv_values(STATE_DIR / ".env")
    ids = []
    for part in (vals.get("ALLOWED_USER_IDS") or "").replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
    return {k: (v or "").strip() for k, v in vals.items()}, ids
