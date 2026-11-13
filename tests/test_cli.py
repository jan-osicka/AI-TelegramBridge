from pathlib import Path


def test_run_dispatches(monkeypatch):
    from telegram_bridge import bridge, cli
    called = []

    async def stub():
        called.append(True)

    monkeypatch.setattr(bridge, "main", stub)
    assert cli.main(["run"]) == 0
    assert called == [True]


def test_home_env_override(monkeypatch, tmp_path):
    from telegram_bridge import settings
    monkeypatch.setenv("TELEGRAM_BRIDGE_HOME", str(tmp_path))
    assert settings.home() == Path(tmp_path)
