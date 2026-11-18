import json

import pytest

from telegram_bridge import cli, doctor, redaction, settings

TOKEN = "123456789:AAFakeTokenValueForTestsOnly_abcdefghij"
TOTP = "JBSWY3DPEHPK3PXP"


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    for sub in ("outbox", "logs", "inbox"):
        (h / sub).mkdir(parents=True)
    work = tmp_path / "work"
    work.mkdir()
    agent = tmp_path / "agent.exe"
    agent.write_text("x")
    monkeypatch.setenv("TELEGRAM_BRIDGE_HOME", str(h))
    for name, sub in (("STATE_DIR", ""), ("OUTBOX", "outbox"), ("LOGS", "logs"), ("LOCK", "bridge.lock")):
        monkeypatch.setattr(settings, name, h / sub if sub else h)
    (h / "config.toml").write_text(
        f'[general]\ncwd = "{work.as_posix()}"\nallowed_cwd_roots = ["{work.as_posix()}"]\n'
        f'outbox_dirs = ["{work.as_posix()}"]\n'
        f'[bots.alpha]\ntoken_env = "ALPHA_TOKEN"\ncommand = "{agent.as_posix()}"\nargs = []\n')
    (h / ".env").write_text(f"ALPHA_TOKEN={TOKEN}\nALLOWED_USER_IDS=11\nTOTP_SECRET={TOTP}\n")
    monkeypatch.setattr(settings, "config_path", lambda: h / "config.toml")
    monkeypatch.setattr(redaction, "_known", [])
    return h


def probes(**kw):
    base = dict(
        run=lambda cmd, timeout: (0, "agent 1.2.3\n"),
        which=lambda n: "/usr/bin/" + n,
        pid_alive=lambda pid: True,
        getme=lambda tok: "alpha_bot",
        http_check=lambda url: True,
        whisper_cached=lambda m: True,
        importable=lambda n: True,
        lock_held=lambda p: True,
        lock_info=lambda p: {"pid": 4242},
        permissions_ok=lambda p: True,
    )
    base.update(kw)
    return doctor.Probes(**base)


def by_name(checks):
    return {c.name: c for c in checks}


def test_healthy_home_has_no_fail(home):
    checks = doctor.run_checks(False, probes=probes())
    assert [c for c in checks if c.status == "FAIL"] == []
    assert by_name(checks)["bridge"].status == "OK"


def test_missing_token_fails_with_hint(home):
    (home / ".env").write_text(f"ALLOWED_USER_IDS=11\nTOTP_SECRET={TOTP}\n")
    c = by_name(doctor.run_checks(False, probes=probes()))["token alpha"]
    assert c.status == "FAIL" and "ALPHA_TOKEN" in c.hint and "missing" in c.detail


def test_bad_totp_fails(home):
    (home / ".env").write_text(f"ALPHA_TOKEN={TOKEN}\nALLOWED_USER_IDS=11\nTOTP_SECRET=not base32 !!\n")
    assert by_name(doctor.run_checks(False, probes=probes()))["totp"].status == "FAIL"


def test_empty_ids_fail(home):
    (home / ".env").write_text(f"ALPHA_TOKEN={TOKEN}\nTOTP_SECRET={TOTP}\n")
    assert by_name(doctor.run_checks(False, probes=probes()))["allowed users"].status == "FAIL"


def test_agent_version_failure_and_missing_cwd(home, tmp_path):
    p = probes(run=lambda cmd, t: (1, "boom"))
    checks = by_name(doctor.run_checks(False, probes=p))
    assert checks["agent alpha"].status == "FAIL"
    (tmp_path / "work").rmdir()
    assert by_name(doctor.run_checks(False, probes=probes()))["cwd"].status == "FAIL"


def test_not_running_autostart_and_permissions_are_warn(home):
    p = probes(lock_held=lambda x: False, permissions_ok=lambda x: False,
               run=lambda cmd, t: (1, "") if "--version" not in cmd else (0, "v1"))
    c = by_name(doctor.run_checks(False, probes=p))
    assert c["bridge"].status == "WARN" and c["autostart"].status == "WARN" and c["permissions"].status == "WARN"


def test_stale_pid_warns(home):
    c = by_name(doctor.run_checks(False, probes=probes(pid_alive=lambda p: False)))
    assert c["bridge"].status == "WARN"


def test_voice_missing_extra_warns(home):
    cfg = (home / "config.toml").read_text().replace("[general]\n", '[general]\nwhisper_model = "tiny"\n', 1)
    (home / "config.toml").write_text(cfg)
    c = by_name(doctor.run_checks(False, probes=probes(importable=lambda n: False)))
    assert c["voice"].status == "WARN" and "uv sync --extra voice" in c["voice"].hint
    c = by_name(doctor.run_checks(False, probes=probes(whisper_cached=lambda m: False, which=lambda n: None)))
    assert c["whisper model"].status == "WARN" and c["ffmpeg"].status == "WARN"


def test_state_files_and_log_errors(home):
    (home / "state.json").write_text("{not json")
    (home / "sessions.json").write_text(json.dumps({}))
    (home / "logs" / "bridge.log").write_text("x INFO ok\n2026 ERROR bad\n2026 ERROR worse\n")
    c = by_name(doctor.run_checks(False, probes=probes()))
    assert c["state.json"].status == "FAIL" and c["sessions.json"].status == "OK"
    assert "2 ERROR" in c["log"].detail


def test_online_getme_and_failure_redacted(home):
    c = by_name(doctor.run_checks(True, probes=probes()))
    assert c["getMe alpha"].detail == "@alpha_bot"

    def boom(tok):
        raise RuntimeError(f"bad url https://api.telegram.org/bot{tok}/getMe")
    c = by_name(doctor.run_checks(True, probes=probes(getme=boom)))["getMe alpha"]
    assert c.status == "FAIL" and "RuntimeError" in c.detail and TOKEN not in c.detail


def test_never_modifies_home(home):
    before = sorted(p.name for p in home.rglob("*"))
    (home / "logs").rmdir()
    (home / "logs").mkdir()
    doctor.run_checks(True, probes=probes())
    assert sorted(p.name for p in home.rglob("*")) == before


def test_cli_output_has_no_secrets_and_exit_codes(home, monkeypatch, capsys):
    p = probes()
    monkeypatch.setattr(doctor, "Probes", lambda: p)
    assert cli.main(["doctor", "--online"]) == 0
    out = capsys.readouterr().out
    assert TOKEN not in out and TOTP not in out and "token alpha: ALPHA_TOKEN: set" in out
    assert out.splitlines()[0].startswith("OK")
    (home / ".env").write_text(f"ALLOWED_USER_IDS=11\nTOTP_SECRET={TOTP}\n")
    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "FAIL token alpha" in out and "hint:" in out and TOTP not in out


def test_hostile_config_never_aborts(home, tmp_path, monkeypatch, capsys):
    work = (tmp_path / "work").as_posix()
    agent = (tmp_path / "agent.exe").as_posix()
    (home / "config.toml").write_text(
        f'[general]\ncwd = "{work}"\noutbox_dirs = 5\nwhisper_model = 7\n'
        f'[local_model]\nstart_command = [5]\nstop_command = ""\n'
        f'[bots.alpha]\ntoken_env = "ALPHA_TOKEN"\ncommand = "{agent}"\nneeds_local_model = true\n'
        f'[bots.beta]\ntoken_env = "BETA"\ncommand = 5\n')

    def boom(*a):
        raise OSError("probe exploded")
    p = probes(permissions_ok=boom, which=boom)
    names = by_name(doctor.run_checks(False, probes=p))
    assert names["home"].status == "FAIL" and "OSError" in names["home"].detail
    assert names["folders"].status == "FAIL"
    assert names["agent beta"].status == "FAIL"
    for later in ("bridge", "autostart", "log", "state.json", "sessions.json", "totp"):
        assert later in names
    monkeypatch.setattr(doctor, "Probes", lambda: p)
    assert cli.main(["doctor"]) == 1
    assert "checks:" in capsys.readouterr().out


def test_real_lock_probe_does_not_create_lock_file(home, monkeypatch):
    monkeypatch.setattr(settings, "LOCK", home / "bridge.lock")
    p = probes()
    p.lock_held = doctor.lock.is_held
    p.lock_info = doctor.lock.read_info
    doctor.run_checks(False, probes=p)
    assert not (home / "bridge.lock").exists()


BODIES = [
    'bots = 5\n[general]\ncwd = "."\n',
    'general = 5\n[bots.a]\ntoken_env = "T"\ncommand = "x"\n',
    '[general]\ncwd = "."\n[bots]\na = 5\n',
]


@pytest.mark.parametrize("body", BODIES)
def test_non_table_sections_do_not_abort(home, monkeypatch, capsys, body):
    (home / "config.toml").write_text(body)
    p = probes()
    names = by_name(doctor.run_checks(False, probes=p))
    assert names["config"].status == "FAIL"
    for later in ("bridge", "autostart", "log", "totp", "state.json"):
        assert later in names
    monkeypatch.setattr(doctor, "Probes", lambda: p)
    assert cli.main(["doctor"]) == 1
    assert "checks:" in capsys.readouterr().out


def test_doctor_reports_voice_replies(home):
    cfg = (home / "config.toml").read_text()
    (home / "config.toml").write_text(cfg + '[bots.alpha.voice]\nvoices = { en = "en-VOICE" }\n')
    checks = by_name(doctor.run_checks(False, probes=probes()))
    assert checks["voice replies"].status == "OK" and checks["voice ffmpeg"].status == "OK"
    assert "en=en-VOICE" in checks["voices alpha"].detail
    checks = by_name(doctor.run_checks(False, probes=probes(importable=lambda n: n != "edge_tts", which=lambda n: None)))
    assert checks["voice replies"].status == "WARN" and checks["voice ffmpeg"].status == "WARN"


def test_doctor_silent_without_voice_table(home):
    assert not any(c.name.startswith("voice") and c.name != "voice" for c in doctor.run_checks(False, probes=probes()))
