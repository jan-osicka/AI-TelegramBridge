import re
from importlib import resources
import sys
import tomllib
from pathlib import Path, PurePosixPath
from unittest import mock

import pytest

from telegram_bridge import cli, service, setupcmd

ROOT = Path(__file__).resolve().parent.parent
WARNING = "This bot can run any command on this computer. The whitelist, 2FA and the safety rules in the prompt are what protect it."


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    monkeypatch.setenv("TELEGRAM_BRIDGE_HOME", str(h))
    monkeypatch.setattr(setupcmd.osutil, "restrict_permissions", lambda p: (True, "ok"))
    return h


def test_setup_creates_tree_and_template(home, capsys):
    assert cli.main(["setup"]) == 0
    for sub in ("inbox", "outbox", "logs"):
        assert (home / sub).is_dir()
    env = (home / ".env").read_text()
    assert env.startswith("CLAUDE_BOT_TOKEN=\nQWEN_BOT_TOKEN=\nALLOWED_USER_IDS=\n")
    m = re.search(r"^TOTP_SECRET=(\S+)$", env, re.M)
    assert m and len(m.group(1)) >= 16
    assert (home / "config.toml").exists()
    out = capsys.readouterr().out
    assert WARNING in out
    assert m.group(1) not in out


def test_setup_idempotent_keeps_values(home):
    cli.main(["setup"])
    env = home / ".env"
    text = env.read_text().replace("CLAUDE_BOT_TOKEN=", "CLAUDE_BOT_TOKEN=abc:123")
    env.write_text(text)
    (home / "config.toml").write_text("# mine\n")
    cli.main(["setup"])
    assert env.read_text() == text
    assert (home / "config.toml").read_text() == "# mine\n"


def test_setup_fills_empty_secret_only(home):
    home.mkdir()
    (home / ".env").write_text("CLAUDE_BOT_TOKEN=t\nTOTP_SECRET=\n")
    cli.main(["setup"])
    t = (home / ".env").read_text()
    assert "CLAUDE_BOT_TOKEN=t\n" in t and re.search(r"TOTP_SECRET=\w{16,}", t)


def test_setup_no_qr_prints_uri_with_warning(home, capsys):
    with mock.patch.dict(sys.modules, {"qrcode": None}):
        cli.main(["setup"])
    out = capsys.readouterr().out
    assert "otpauth://totp/" in out and "Telegram%20Bridge" in out
    assert "delete" in out.lower()


def test_setup_whisper_mocked(home):
    with mock.patch.object(setupcmd, "download_whisper", return_value=0) as d:
        cli.main(["setup", "--whisper"])
    d.assert_called_once()


def test_example_config_parses_and_is_generic():
    text = resources.files("telegram_bridge").joinpath("config.example.toml").read_text(encoding="utf-8")
    cfg = tomllib.loads(text)
    assert cfg["bots"]["claude"]["display_name"] == "Claude"
    assert "bypassPermissions" in cfg["bots"]["claude"]["args"]
    assert WARNING in text
    low = text.lower()
    for bad in ("ro" + "ok", "vio" + "let", "par" + "ty", "janos" + "icka", "c:" + chr(92) + "users"):
        assert bad not in low


@pytest.mark.parametrize("plat,needle", [("win32", "Register-ScheduledTask"), ("linux", "ExecStart="), ("darwin", "KeepAlive")])
def test_service_print(monkeypatch, capsys, plat, needle):
    monkeypatch.setattr(service, "PLATFORM", plat)
    exe = PurePosixPath("/v/Scripts/telegram-bridge.exe" if plat == "win32" else "/v/bin/telegram-bridge")
    monkeypatch.setattr(service, "exe_path", lambda: exe)
    assert cli.main(["service", "print"]) == 0
    out = capsys.readouterr().out
    assert needle in out
    if plat == "darwin":
        assert "<string>run</string>" in out and "<string>--service</string>" in out
    else:
        assert "run --service" in out
    assert str(exe) in out
    if plat == "win32":
        for s in ("-RestartCount 999", "-Hidden", "-RunLevel Limited", "TelegramBridge"):
            assert s in out
    if plat == "linux":
        assert "Restart=on-failure" in out and "RestartSec=60" in out
    assert "TOTP" not in out and "TOKEN" not in out


def test_service_install_windows_mocked(monkeypatch):
    monkeypatch.setattr(service, "PLATFORM", "win32")
    monkeypatch.setattr(service, "exe_path", lambda: Path("C:/v/Scripts/telegram-bridge.exe"))
    with mock.patch.object(service.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="", stderr="")) as r:
        assert cli.main(["service", "install"]) == 0
        assert r.call_args[0][0][0] == "powershell.exe"
        assert cli.main(["service", "remove"]) == 0
        assert "Unregister-ScheduledTask" in " ".join(r.call_args[0][0])


def test_service_install_linux_mocked(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "PLATFORM", "linux")
    monkeypatch.setattr(service.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(service, "exe_path", lambda: Path("/v/bin/telegram-bridge"))
    with mock.patch.object(service.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="", stderr="")) as r:
        assert cli.main(["service", "install"]) == 0
    assert (tmp_path / ".config/systemd/user/telegram-bridge.service").exists()
    cmds = [c[0][0] for c in r.call_args_list]
    assert ["systemctl", "--user", "daemon-reload"] in cmds
    assert ["systemctl", "--user", "enable", "--now", "telegram-bridge.service"] in cmds


def test_exe_missing_fails(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    assert cli.main(["service", "print"]) == 1
    assert "not found" in capsys.readouterr().err


def test_example_config_in_package_and_repo_has_no_copy():
    assert resources.files("telegram_bridge").joinpath("config.example.toml").is_file()
    assert not (ROOT / "config.example.toml").exists()


def test_setup_copies_packaged_config(home):
    cli.main(["setup"])
    assert tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))["bots"]["claude"]


def test_totp_account_is_generic(home, capsys):
    with mock.patch.dict(sys.modules, {"qrcode": None}):
        cli.main(["setup"])
    assert "telegram-bridge?secret=" in capsys.readouterr().out


ODD = PurePosixPath("/p a'th & <x>/bin/telegram-bridge")
ODDW = PurePosixPath("/w d'ir & <y>")


def test_renderers_quote_odd_paths():
    u = service.systemd_unit(ODD, ODDW)
    assert 'ExecStart="' + str(ODD) + '" run --service' in u
    assert "WorkingDirectory=" + str(ODDW) + chr(10) in u
    assert "Wants=network-online.target" in u
    p = service.launchd_plist(ODD, ODDW)
    assert "p a&apos;th" not in p and "&amp; &lt;x&gt;" in p and "&amp; &lt;y&gt;" in p
    import xml.dom.minidom
    xml.dom.minidom.parseString(p)
    w = service.windows_script(ODD, ODDW)
    assert "'/p a''th & <x>/bin/telegram-bridge'" in w and "'/w d''ir & <y>'" in w
    assert 'a"b' not in service._sd_quote('a"b') and service._sd_quote("50%") == '"50%%"'
