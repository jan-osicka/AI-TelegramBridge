"""`telegram-bridge doctor`: read-only health checks. Output is safe to paste (redacted, no secrets).

Never modifies anything: no mkdir, no lock acquisition, no permission changes.
"""
import asyncio
import importlib.util
import json
import os
import shutil
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from . import lock, osutil, redaction, settings

Status = Literal["OK", "WARN", "FAIL"]


@dataclass
class Check:
    name: str
    status: Status
    detail: str
    hint: str = ""


def _pid_alive(pid: int) -> bool:
    if osutil.IS_WINDOWS:
        try:
            r = osutil.run_hidden(["tasklist", "/FI", f"PID eq {pid}", "/NH"], 15)
        except Exception:
            return False
        return str(pid) in (r.stdout or b"").decode("utf-8", "replace").split()
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _run(cmd, timeout):
    r = osutil.run_hidden(cmd, timeout)
    return r.returncode, ((r.stdout or b"") + (r.stderr or b"")).decode("utf-8", "replace")


def _getme(token: str) -> str:
    from telegram import Bot
    from telegram.request import HTTPXRequest

    async def go():
        req = HTTPXRequest(connect_timeout=10, read_timeout=10, write_timeout=10, pool_timeout=10)
        async with Bot(token, request=req) as bot:
            return (await bot.get_me()).username
    return asyncio.run(go())


def _http_check(url):
    from . import localmodel
    return localmodel.http_check(url)


def _hf_cache_dirs() -> list[Path]:
    hub = os.environ.get("HF_HUB_CACHE")
    if hub:
        return [Path(hub)]
    base = os.environ.get("HF_HOME")
    return [Path(base) / "hub" if base else Path.home() / ".cache" / "huggingface" / "hub"]


def _whisper_cached(model: str) -> bool:
    """Offline lookup: a models--*faster-whisper-<model>* folder with a snapshot in the Hugging Face cache."""
    if Path(model).is_dir():
        return True
    want = f"faster-whisper-{model}".lower()
    for d in _hf_cache_dirs():
        try:
            for p in d.glob("models--*"):
                if want in p.name.lower() and any((p / "snapshots").glob("*/*")):
                    return True
        except OSError:
            continue
    return False


def _autostart_cmd():
    if osutil.IS_WINDOWS:
        return ["schtasks", "/Query", "/TN", "TelegramBridge"]
    if sys.platform == "darwin":
        return ["launchctl", "list", "com.telegram-bridge"]
    return ["systemctl", "--user", "is-enabled", "telegram-bridge"]


@dataclass
class Probes:
    """Everything that touches processes, the network or the OS; tests replace these."""
    run: Callable = _run                      # (cmd, timeout) -> (returncode, output)
    which: Callable = shutil.which
    pid_alive: Callable = _pid_alive
    getme: Callable = _getme                  # (token) -> username
    http_check: Callable = _http_check        # (url) -> bool
    whisper_cached: Callable = _whisper_cached
    importable: Callable = lambda n: importlib.util.find_spec(n) is not None
    lock_held: Callable = lock.is_held
    lock_info: Callable = lock.read_info
    permissions_ok: Callable = osutil.permissions_ok


def _exe_exists(cmd, probes) -> bool:
    return bool(cmd) and (Path(cmd).exists() or bool(probes.which(cmd)))


def run_checks(online: bool = False, env: dict | None = None, probes: Probes | None = None) -> list[Check]:
    probes = probes or Probes()
    out: list[Check] = []

    def add(name, status, detail, hint=""):
        out.append(Check(name, status, detail, hint))

    @contextmanager
    def guard(name):
        """Any exception in a section becomes a FAIL check, so doctor always finishes."""
        try:
            yield
        except Exception as e:
            add(name, "FAIL", f"check crashed ({type(e).__name__}): {e}", "check the config for malformed values")

    home = settings.STATE_DIR
    cfg = None
    try:
        cfg = settings.load_config()
        for key in ("general", "bots", "local_model"):
            if not isinstance(cfg.get(key) or {}, dict):
                add("config", "FAIL", f"{key} is not a table", f"make [{key}] a table in config.toml")
                cfg[key] = {}
        for k in [k for k, b in cfg["bots"].items() if not isinstance(b, dict)]:
            add("config", "FAIL", f"bot {k} is not a table", f"make [bots.{k}] a table")
            del cfg["bots"][k]
        bots = cfg.get("bots") or {}
        if not bots:
            add("config", "FAIL", "no [bots.*] section", f"add at least one bot to {home / 'config.toml'}")
        elif not (cfg.get("general") or {}).get("cwd"):
            add("config", "FAIL", "[general] cwd is missing", "set cwd in [general]")
        else:
            missing = [k for k, b in bots.items() if not b.get("command") or not b.get("token_env")]
            if missing:
                add("config", "FAIL", f"bots without command/token_env: {', '.join(missing)}",
                    "each bot needs command and token_env")
            else:
                add("config", "OK", f"{len(bots)} bot(s): {', '.join(bots)}")
    except FileNotFoundError:
        add("config", "FAIL", "config.toml not found", f"run `telegram-bridge setup` or create {home / 'config.toml'}")
    except Exception as e:
        add("config", "FAIL", f"cannot parse config ({type(e).__name__}): {e}", "fix the TOML syntax")

    with guard("home"):
        envfile = home / ".env"
        if home.is_dir():
            add("home", "OK", str(home))
        else:
            add("home", "FAIL", f"{home} does not exist", "run `telegram-bridge setup`")
        if envfile.is_file():
            add(".env", "OK", "exists")
        else:
            add(".env", "FAIL", "missing", "run `telegram-bridge setup` and fill in the tokens")
        if home.is_dir():
            bad = [p.name for p in (home, envfile) if p.exists() and not probes.permissions_ok(p)]
            if bad:
                add("permissions", "WARN", f"not restricted to the owner: {', '.join(bad)}",
                    "run `telegram-bridge setup` (it restricts permissions)")
            else:
                add("permissions", "OK", "owner only")

    secrets, ids = {}, []
    if envfile.is_file():
        try:
            secrets, ids = settings.load_secrets()
        except Exception as e:
            add(".env parse", "FAIL", f"cannot read ({type(e).__name__})")
    if env is not None:
        secrets = {**secrets, **env}
    tokens = {}
    with guard("tokens"):
        if cfg:
            for name, b in (cfg.get("bots") or {}).items():
                with guard(f"token {name}"):
                    te = b.get("token_env", "")
                    if secrets.get(te):
                        tokens[name] = secrets[te]
                        add(f"token {name}", "OK", f"{te}: set")
                    else:
                        add(f"token {name}", "FAIL", f"{te}: missing", f"add {te}=<token from @BotFather> to {envfile}")
    with guard("allowed users"):
        if ids:
            add("allowed users", "OK", f"{len(ids)} id(s)")
        else:
            add("allowed users", "FAIL", "ALLOWED_USER_IDS is empty", f"add ALLOWED_USER_IDS=<your numeric id> to {envfile}")
    with guard("totp"):
        totp = secrets.get("TOTP_SECRET", "")
        if not totp:
            add("totp", "FAIL", "TOTP_SECRET missing", "run `telegram-bridge setup`")
        else:
            try:
                import pyotp
                pyotp.TOTP(totp).now()
                add("totp", "OK", "valid base32 secret")
            except Exception:
                add("totp", "FAIL", "TOTP_SECRET is not valid base32", "run `telegram-bridge setup` to make a new one")

    if cfg:
        g = cfg.get("general") or {}
        bots = cfg.get("bots") or {}
        seen = set()
        for name, b in bots.items():
            with guard(f"agent {name}"):
                cmd = b.get("command")
                if not cmd or cmd in seen:
                    continue
                seen.add(cmd)
                if not _exe_exists(cmd, probes):
                    add(f"agent {name}", "FAIL", f"command not found: {cmd}", "install the agent CLI or fix `command`")
                    continue
                try:
                    rc, text = probes.run([cmd, "--version"], 20)
                except Exception as e:
                    add(f"agent {name}", "FAIL", f"--version failed ({type(e).__name__})", "run the command by hand to see why")
                    continue
                if rc == 0:
                    add(f"agent {name}", "OK", (text.strip().splitlines() or ["ok"])[0][:80])
                else:
                    add(f"agent {name}", "FAIL", f"--version exited {rc}", "run the command by hand to see why")
        with guard("cwd"):
            cwd = g.get("cwd")
            if cwd:
                ok = Path(cwd).expanduser().is_dir()
                add("cwd", "OK" if ok else "FAIL", str(cwd) if ok else f"{cwd} does not exist",
                    "" if ok else "create the folder or change [general] cwd")
        with guard("folders"):
            for key in ("allowed_cwd_roots", "outbox_dirs"):
                for d in g.get(key) or []:
                    ok = Path(d).expanduser().is_dir()
                    add(key, "OK" if ok else "FAIL", d if ok else f"{d} does not exist",
                        "" if ok else f"create the folder or remove it from {key}")
        with guard("local model"):
            lm = cfg.get("local_model") or {}
            if any(b.get("needs_local_model") for b in bots.values()):
                for key in ("start_command", "stop_command"):
                    c = lm.get(key)
                    if not c:
                        add(f"local model {key}", "WARN", "not set",
                            "the bridge cannot start the local model by itself" if key == "start_command"
                            else "the bridge cannot stop the local model by itself")
                        continue
                    exe = c[0] if isinstance(c, list) else str(c).split()[0]
                    ok = _exe_exists(exe, probes)
                    add(f"local model {key}", "OK" if ok else "FAIL", exe if ok else f"not found: {exe}",
                        "" if ok else f"fix {key} in [local_model]")
        with guard("voice"):
            if g.get("whisper_model"):
                if not probes.importable("faster_whisper"):
                    add("voice", "WARN", "faster-whisper is not installed", "uv sync --extra voice")
                else:
                    add("voice", "OK", "faster-whisper installed")
                    m = g["whisper_model"]
                    if probes.whisper_cached(m):
                        add("whisper model", "OK", f"{m} cached")
                    else:
                        add("whisper model", "WARN", f"{m} is not in the local cache",
                            "it downloads on the first voice message")
                if probes.which("ffmpeg"):
                    add("ffmpeg", "OK", "on PATH")
                else:
                    add("ffmpeg", "WARN", "not on PATH", "install ffmpeg (voice messages need it)")
        with guard("voice replies"):
            vbots = {n: b for n, b in bots.items() if b.get("voice") or b.get("voice_problems")}
            if any(b.get("voice") for b in vbots.values()):
                if probes.importable("edge_tts"):
                    add("voice replies", "OK", "edge-tts installed")
                else:
                    add("voice replies", "WARN", "edge-tts is not installed", "uv sync --extra tts")
                if probes.which("ffmpeg"):
                    add("voice ffmpeg", "OK", "on PATH")
                else:
                    add("voice ffmpeg", "WARN", "not on PATH", "install ffmpeg (voice replies need it)")
            for n, b in vbots.items():
                v, bad = b.get("voice") or {"voices": {}, "default_lang": "-"}, b.get("voice_problems")
                add(f"voices {n}", "WARN" if bad else "OK",
                    ", ".join(f"{k}={x}" for k, x in v["voices"].items()) + f" (default {v['default_lang']})"
                    + (f"; {'; '.join(bad)}" if bad else ""),
                    f"check [bots.{n}.voice] in config.toml" if bad else "")

    try:
        if probes.lock_held(settings.LOCK):
            pid = (probes.lock_info(settings.LOCK) or {}).get("pid")
            if isinstance(pid, int) and not probes.pid_alive(pid):
                add("bridge", "WARN", f"lock held but pid {pid} not found")
            else:
                add("bridge", "OK", f"running (pid {pid})" if pid else "running")
        else:
            add("bridge", "WARN", "not running", "start it with `telegram-bridge run` or the autostart")
    except Exception as e:
        add("bridge", "WARN", f"cannot tell ({type(e).__name__})")
    try:
        rc, _ = probes.run(_autostart_cmd(), 20)
        if rc == 0:
            add("autostart", "OK", "registered")
        else:
            add("autostart", "WARN", "not registered", "run `telegram-bridge service install`")
    except Exception as e:
        add("autostart", "WARN", f"cannot tell ({type(e).__name__})", "run `telegram-bridge service install`")

    with guard("log"):
        logf = settings.LOGS / "bridge.log"
        if logf.is_file():
            try:
                lines = logf.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
                n = sum(1 for ln in lines if " ERROR " in ln or ln.startswith("ERROR"))
                add("log", "OK" if n == 0 else "WARN", f"{n} ERROR line(s) in the last {len(lines)}",
                    "" if n == 0 else f"see {logf}")
            except OSError as e:
                add("log", "WARN", f"cannot read bridge.log ({type(e).__name__})")
        else:
            add("log", "OK", "no bridge.log yet")
    for fn in ("state.json", "sessions.json"):
        with guard(fn):
            p = home / fn
            if not p.exists():
                add(fn, "OK", "not created yet")
                continue
            try:
                json.loads(p.read_text(encoding="utf-8"))
                add(fn, "OK", "parses")
            except (OSError, ValueError) as e:
                add(fn, "FAIL", f"does not parse ({type(e).__name__})", f"fix or move {p}")

    if online:
        for name, tok in tokens.items():
            try:
                add(f"getMe {name}", "OK", "@" + str(probes.getme(tok)))
            except Exception as e:
                add(f"getMe {name}", "FAIL", f"{type(e).__name__}: {e}", "check the token and the network")
        with guard("local model"):
            if cfg and any(b.get("needs_local_model") for b in (cfg.get("bots") or {}).values()):
                url = (cfg.get("local_model") or {}).get("url")
                if not url:
                    add("local model", "WARN", "no url configured")
                elif probes.http_check(url):
                    add("local model", "OK", f"{url} lists models")
                else:
                    add("local model", "WARN", "not running", "the bridge starts it on demand")

    known = [*redaction.secret_values(secrets), *tokens.values()]
    for c in out:
        c.detail = redaction.redact(c.detail, known)[0]
        c.hint = redaction.redact(c.hint, known)[0]
    return out


def format_check(c: Check) -> str:
    s = f"{c.status:4} {c.name}: {c.detail}"
    return s + (f"\n     hint: {c.hint}" if c.hint else "")


def run(args) -> int:
    checks = run_checks(online=getattr(args, "online", False))
    for c in checks:
        print(format_check(c))
    fails = sum(c.status == "FAIL" for c in checks)
    warns = sum(c.status == "WARN" for c in checks)
    print(f"\n{len(checks)} checks: {fails} FAIL, {warns} WARN")
    return 1 if fails else 0
