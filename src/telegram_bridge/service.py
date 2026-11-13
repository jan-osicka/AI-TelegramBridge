"""Autostart: Windows Scheduled Task, systemd user unit or launchd agent."""
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from . import settings

PLATFORM = sys.platform
TASK = "TelegramBridge"
UNIT = "telegram-bridge.service"
LABEL = "com.telegram-bridge"


class ServiceError(Exception):
    pass


def exe_path() -> Path:
    """The telegram-bridge launcher inside the running virtual environment."""
    prefix = Path(sys.prefix)
    if PLATFORM == "win32":
        exe = prefix / "Scripts" / "telegram-bridge.exe"
    else:
        exe = prefix / "bin" / "telegram-bridge"
    if not exe.exists():
        raise ServiceError(f"{exe} not found. Install the package into its environment first (for example: uv sync).")
    return exe


def _ps(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def windows_script(exe: Path, workdir: Path) -> str:
    return "\n".join([
        '$ErrorActionPreference = "Stop"',
        f'$name = "{TASK}"',
        f"$action = New-ScheduledTaskAction -Execute {_ps(str(exe))} -Argument 'run --service' -WorkingDirectory {_ps(str(workdir))}",
        "$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME",
        "$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `",
        "    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `",
        "    -ExecutionTimeLimit ([TimeSpan]::Zero) -Hidden",
        "$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited",
        "Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `",
        "    -Principal $principal -Force | Out-Null",
        "Write-Host \"Registered scheduled task '$name' (at logon, hidden, restart on failure).\"",
        "",
    ])


def _sd_quote(path) -> str:
    """Quote a path as one systemd command word (escape backslash, quote and %)."""
    v = str(path)
    if "\n" in v:
        raise ServiceError("path contains a newline")
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def systemd_unit(exe: Path, workdir: Path) -> str:
    wd = str(workdir)
    if "\n" in wd:
        raise ServiceError("path contains a newline")
    return (
        "[Unit]\nDescription=Telegram Bridge\nWants=network-online.target\nAfter=network-online.target\n\n"
        f"[Service]\nExecStart={_sd_quote(exe)} run --service\nWorkingDirectory={wd.replace('%', '%%')}\n"
        "Restart=on-failure\nRestartSec=60\n\n[Install]\nWantedBy=default.target\n"
    )


def launchd_plist(exe: Path, workdir: Path) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n<dict>\n'
        f"  <key>Label</key><string>{LABEL}</string>\n"
        "  <key>ProgramArguments</key>\n  <array>\n"
        f"    <string>{escape(str(exe))}</string>\n    <string>run</string>\n    <string>--service</string>\n  </array>\n"
        f"  <key>WorkingDirectory</key><string>{escape(str(workdir))}</string>\n"
        "  <key>RunAtLoad</key><true/>\n"
        "  <key>KeepAlive</key>\n  <dict><key>SuccessfulExit</key><false/></dict>\n"
        "</dict>\n</plist>\n"
    )


def _unit_file() -> Path:
    return Path.home() / ".config/systemd/user" / UNIT


def _plist_file() -> Path:
    return Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"


def _run(cmd) -> bool:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"command failed: {' '.join(map(str, cmd))}\n{(r.stderr or r.stdout).strip()}", file=sys.stderr)
        return False
    return True


def _ps_run(script: str) -> bool:
    return _run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script])


def do_print() -> int:
    exe, wd = exe_path(), settings.CODE_DIR
    if PLATFORM == "win32":
        print(windows_script(exe, wd))
    elif PLATFORM == "darwin":
        print(launchd_plist(exe, wd))
    else:
        print(systemd_unit(exe, wd))
    return 0


def do_install() -> int:
    exe, wd = exe_path(), settings.CODE_DIR
    if PLATFORM == "win32":
        ok = _ps_run(windows_script(exe, wd))
        if ok:
            print(f"Registered task {TASK}. Start now: Start-ScheduledTask -TaskName {TASK}")
    elif PLATFORM == "darwin":
        f = _plist_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(launchd_plist(exe, wd), encoding="utf-8")
        ok = _run(["launchctl", "load", "-w", str(f)])
        print(f"Installed {f}")
    else:
        f = _unit_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(systemd_unit(exe, wd), encoding="utf-8")
        ok = _run(["systemctl", "--user", "daemon-reload"]) and _run(["systemctl", "--user", "enable", "--now", UNIT])
        print(f"Installed {f}")
    return 0 if ok else 1


def do_remove() -> int:
    if PLATFORM == "win32":
        ok = _ps_run(f"Unregister-ScheduledTask -TaskName {TASK} -Confirm:$false")
    elif PLATFORM == "darwin":
        f = _plist_file()
        ok = _run(["launchctl", "unload", "-w", str(f)])
        f.unlink(missing_ok=True)
    else:
        f = _unit_file()
        ok = _run(["systemctl", "--user", "disable", "--now", UNIT])
        f.unlink(missing_ok=True)
        _run(["systemctl", "--user", "daemon-reload"])
    return 0 if ok else 1


def run(args) -> int:
    try:
        return {"print": do_print, "install": do_install, "remove": do_remove}[args.action]()
    except ServiceError as e:
        print(str(e), file=sys.stderr)
        return 1
