from pathlib import Path

import pytest

from telegram_bridge import bridge, settings


def write(tmp_path, text):
    p = tmp_path / "config.toml"
    p.write_text(text, encoding="utf-8")
    return p


def test_placeholders_expand(tmp_path):
    p = write(tmp_path, '''
[general]
cwd = "{home}/work"
extra = "{cwd}/sub"
code = "{code_dir}/x"
[bots.a]
args = ["--resume", "{session_id}", "{prompt}"]
''')
    cfg = settings.load_config(p)
    assert cfg["general"]["cwd"] == f"{settings.home()}/work"
    assert cfg["general"]["extra"] == f"{settings.home()}/work/sub"
    assert cfg["general"]["code"] == f"{settings.CODE_DIR}/x"
    assert cfg["bots"]["a"]["args"] == ["--resume", "{session_id}", "{prompt}"]


def test_legacy_qwen_server_alias(tmp_path):
    p = write(tmp_path, '''
[general]
strata_url = "http://127.0.0.1:8081"
qwen_idle_stop_minutes = 7
qwen_startup_timeout_minutes = 3
qwen_busy_file = "~/busy"
[qwen_server]
start_command = ["a"]
stop_command = ["b"]
[bots.q]
needs_strata = true
''')
    lm = settings.load_config(p)["local_model"]
    assert lm["url"] == "http://127.0.0.1:8081"
    assert lm["start_command"] == ["a"] and lm["stop_command"] == ["b"]
    assert lm["idle_stop_minutes"] == 7 and lm["startup_timeout_minutes"] == 3
    assert lm["busy_file"] == str(Path("~/busy").expanduser())
    assert settings.load_config(p)["bots"]["q"]["needs_local_model"] is True


def test_local_model_section_and_defaults(tmp_path):
    p = write(tmp_path, '[local_model]\nurl = "http://x"\nstart_command = ["s"]\nstop_command = ["t"]\n')
    lm = settings.load_config(p)["local_model"]
    assert lm["idle_stop_minutes"] == 20 and lm["startup_timeout_minutes"] == 6 and lm["busy_file"] is None


def test_display_name_default(tmp_path):
    cfg = settings.load_config(write(tmp_path, '[bots.claude]\ntoken_env = "T"\n'))
    b = cfg["bots"]["claude"]
    assert b["display_name"] == "Claude"
    assert b["mode"] == "agent" and b["needs_local_model"] is False and b["chat_model"] == "local"


def test_mode_and_chat_model(tmp_path):
    cfg = settings.load_config(write(tmp_path, '''
[bots.q]
mode = "auto"
[bots.q.env]
ANTHROPIC_MODEL = "m1"
[bots.r]
mode = "chat"
'''))
    assert cfg["bots"]["q"]["mode"] == "auto" and cfg["bots"]["q"]["chat_model"] == "m1"
    assert cfg["bots"]["r"]["mode"] == "chat"


def test_memory_section_normalised(tmp_path):
    cfg = settings.load_config(write(tmp_path, '''
[general]
cwd = "{home}/w"
[bots.a]
[bots.b]
agent_triggers = ["foo"]
[bots.b.memory]
enabled = true
dir = "~/notes"
save_prompt = "save to {cwd}"
'''))
    a, b = cfg["bots"]["a"]["memory"], cfg["bots"]["b"]["memory"]
    assert a == {"enabled": False, "dir": "", "save_prompt": "", "notice_text": settings.DEFAULT_MEMORY_NOTICE}
    assert settings.DEFAULT_MEMORY_NOTICE == "💾 Saving this to memory (agent run follows)."
    assert b["enabled"] is True and "~" not in b["dir"] and b["dir"].endswith("notes")
    assert b["save_prompt"].startswith("save to ") and "{cwd}" not in b["save_prompt"]
    assert cfg["bots"]["b"]["agent_triggers"] == ["foo"] and cfg["bots"]["a"]["save_triggers"] == []


def test_modelstart_alias_commands():
    names = [c for c, _ in bridge.commands_for({"needs_local_model": True})]
    assert "modelstart" in names and "modelstop" in names
    assert "qwenstart" not in names and "qwenstop" not in names
    assert "modelstart" not in [c for c, _ in bridge.commands_for({"needs_local_model": False})]


def test_make_local_manager_none_without_local_bot():
    assert bridge.make_local_manager({"bots": {"a": {"needs_local_model": False}}, "local_model": {}}) is None


def test_make_local_manager_missing_url_is_clear_error():
    with pytest.raises(ValueError, match=r"\[local_model\] url"):
        bridge.make_local_manager({"bots": {"a": {"needs_local_model": True}}, "local_model": {}})
