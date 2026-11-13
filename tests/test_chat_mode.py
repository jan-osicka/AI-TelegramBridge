import asyncio
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_bridge import chat as chatmod
from telegram_bridge import media
from telegram_bridge import settings
from telegram_bridge.auth import Auth
from telegram_bridge.bridge import BotService


@pytest.mark.parametrize("text,mode,expect", [
    ("Ahoj, jak se máš?", "auto", "chat"),
    ("Kolik je 17*23?", "auto", "chat"),
    ("Spusť pytest v projektu", "auto", "agent"),
    ("vytvoř soubor hello.txt", "auto", "agent"),
    ("Install numpy please", "auto", "agent"),
    ("najdi na webu počasí", "auto", "agent"),
    (r"co je v C:\AI\x","auto", "agent"),
    ("ahoj", "agent", "agent"),
    ("spusť to", "chat", "chat"),
    ("!ahoj", "chat", "agent"),
    ("!ahoj", "auto", "agent"),
    ("? spusť pytest", "auto", "chat"),
    ("?spusť pytest", "agent", "chat"),
])
def test_route(text, mode, expect):
    assert chatmod.route(text, mode)[0] == expect


def test_route_strips_prefix_and_files():
    assert chatmod.route("! spusť to", "auto") == ("agent", "spusť to")
    assert chatmod.route("?ahoj", "auto") == ("chat", "ahoj")
    assert chatmod.route("ahoj", "chat", has_file=True)[0] == "agent"
    assert chatmod.route("co je na obrázku", "auto", has_image=True)[0] == "chat"


def test_brain_summary_truncated_readme_first(tmp_path):
    (tmp_path / "zzz.md").write_text("z" * 50000)
    (tmp_path / "README.md").write_text("readme text")
    out = chatmod.brain_summary(tmp_path)
    assert out.startswith("## README.md") and len(out) <= chatmod.BRAIN_MAX_CHARS + 50


def test_history_trim_and_image_placeholder():
    s = chatmod.ChatSessions(max_messages=4)
    for i in range(5):
        s.messages(1, "sys", [{"type": "text", "text": "x"}] if i == 4 else f"q{i}")
        s.add_reply(1, f"a{i}", f"q{i}")
    h = s.history[1]
    assert len(h) <= 4 and h[0]["role"] == "user"
    assert all(isinstance(m["content"], str) for m in h)


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat, text, **kw):
        self.sent.append(text)

    async def send_chat_action(self, chat, action):
        pass


def make_svc():
    cfg = settings.load_config()
    s = BotService("qwen", cfg["bots"]["qwen"], "t", Auth([1], "JBSWY3DPEHPK3PXP"), cfg["general"], qwen=None)
    s.app = SimpleNamespace(bot=FakeBot())
    return s


def test_default_mode_is_configured_and_claude_stays_agent():
    cfg = settings.load_config()
    assert cfg["bots"]["qwen"]["mode"] in chatmod.MODES
    s = make_svc()
    assert s.chat_enabled
    r = BotService("claude", cfg["bots"]["claude"], "t", Auth([1], "JBSWY3DPEHPK3PXP"), cfg["general"])
    assert not r.chat_enabled and r.mode == "agent"


def test_chat_history_kept_and_cleared_by_exit(monkeypatch):
    s = make_svc()
    seen = []

    async def fake_complete(url, messages, **kw):
        seen.append(list(messages))
        return f"reply{len(seen)}"
    monkeypatch.setattr(chatmod, "complete", fake_complete)

    async def go():
        await s.run_chat(5, {"mode": "chat", "text": "ahoj", "prompt": "ahoj", "images": []})
        await s.run_chat(5, {"mode": "chat", "text": "a ty?", "prompt": "a ty?", "images": []})
        assert [m["content"] for m in seen[1][1:]] == ["ahoj", "reply1", "a ty?"]
        assert "Qwen" in seen[0][0]["content"]
        msg = SimpleNamespace(reply_text=lambda t: asyncio.sleep(0))
        await s.command(SimpleNamespace(effective_message=msg), "exit", "", 1, 5)
        assert 5 not in s.chats.history
        await s.run_chat(5, {"mode": "chat", "text": "znovu", "prompt": "znovu", "images": []})
        assert [m["content"] for m in seen[2][1:]] == ["znovu"]
    asyncio.run(go())
    assert s.app.bot.sent == ["reply1", "reply2", "reply3"]


def test_chat_image_sent_as_image_content(monkeypatch, tmp_path):
    s = make_svc()
    img = tmp_path / "p.jpg"
    img.write_bytes(b"\xff\xd8fake")
    seen = []

    async def fake_complete(url, messages, **kw):
        seen.append(messages)
        return "vidim"
    monkeypatch.setattr(chatmod, "complete", fake_complete)
    asyncio.run(s.run_chat(5, {"mode": "chat", "text": "co to je", "prompt": "", "images": [img]}))
    parts = seen[0][-1]["content"]
    assert parts[1]["type"] == "image_url" and parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert s.chats.history[5][0]["content"] == "co to je [image]"


def test_chat_failure_rolls_back_user_turn(monkeypatch):
    s = make_svc()

    async def boom(*a, **k):
        raise OSError("down")
    monkeypatch.setattr(chatmod, "complete", boom)
    asyncio.run(s.run_chat(5, {"mode": "chat", "text": "x", "prompt": "x", "images": []}))
    assert s.chats.history.get(5) == [] and "failed" in s.app.bot.sent[0]


def test_complete_disables_thinking():
    captured = {}

    def post(url, body, timeout):
        captured.update(body)
        return {"choices": [{"message": {"content": " hi "}}]}
    assert asyncio.run(chatmod.complete("http://x", [], "m", post=post)) == "hi"
    assert captured["chat_template_kwargs"] == {"enable_thinking": False}


def test_whisper_preload_does_not_block():
    gate = threading.Event()
    t0 = time.monotonic()
    th = media.preload("m", loader=lambda name: gate.wait(5))
    assert time.monotonic() - t0 < 0.5 and th.is_alive() and th.daemon
    gate.set()
    th.join(2)


def test_whisper_preload_swallows_errors():
    def bad(name):
        raise RuntimeError("no model")
    th = media.preload("m", loader=bad)
    th.join(2)
    assert not th.is_alive()


def test_auto_followup_sticks_to_agent():
    assert chatmod.route("Great do it", "auto")[0] == "chat"
    assert chatmod.route("Great do it", "auto", last_mode="agent")[0] == "agent"
    assert chatmod.route("?hi", "auto", last_mode="agent")[0] == "chat"


def test_agent_turn_visible_to_chat_and_prefix():
    s = chatmod.ChatSessions()
    s.add_turn(1, "plan LinkedIn", "Plan... Want me to build it?")
    msgs = s.messages(1, "sys", "Great do it")
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert "plan LinkedIn" in chatmod.history_prefix([("user", "plan LinkedIn")])
    assert chatmod.history_prefix([]) == ""


def test_wants_save_rules():
    for yes in ["Zapamatuj si, že bydlím v Brně", "remember that I prefer tea", "Od teď mi tykej, prosím", "Jmenuji se Honza",
                "ulož to do brain?", "my name is Jan", "I live in Prague"]:
        assert chatmod.wants_save(yes), yes
    for no in ["Ahoj, jak se máš?", "Co je hlavní město Francie?", "Mám rád kávu?", "dík", "what time is it", "Pracuji na tom?"]:
        assert not chatmod.wants_save(no), no


def test_brain_limit_keeps_readme_first_and_fits_window(tmp_path):
    (tmp_path / "a.md").write_text("A" * 100)
    (tmp_path / "README.md").write_text("R" * 100)
    out = chatmod.brain_summary(tmp_path, 120)
    assert out.startswith("## README.md")
    assert chatmod.BRAIN_MAX_CHARS >= 9000 and chatmod.BRAIN_MAX_CHARS // 3 < 20000


def test_chat_save_request_queues_agent_save_and_is_honest(monkeypatch):
    s = make_svc()

    async def fake_complete(url, messages, **kw):
        return "Dobře."
    monkeypatch.setattr(chatmod, "complete", fake_complete)

    async def go():
        await s.run_chat(5, {"mode": "chat", "text": "Zapamatuj si, že bydlím v Brně", "prompt": "x", "images": [], "uid": 5})
        await s.run_chat(5, {"mode": "chat", "text": "Ahoj", "prompt": "x", "images": [], "uid": 5})
    asyncio.run(go())
    assert "Saving this to the brain" in s.app.bot.sent[0] and "Saving" not in s.app.bot.sent[1]
    assert s.queue.qsize() == 1
    chat, uid, item = s.queue.get_nowait()
    assert item["save"] and item["mode"] == "agent" and "agent name: Qwen" in item["prompt"]


def test_save_item_does_not_stick_agent_mode(monkeypatch):
    s = make_svc()
    s.unseen[5] = [("user", "Jmenuji se Honza"), ("assistant", "Ok")]
    seen = []

    async def fake_task(chat, prompt, reply_to=None, user_text=None):
        seen.append((prompt, user_text))
        s.last_mode[chat] = "agent"
    monkeypatch.setattr(s, "run_task", fake_task)
    asyncio.run(s.run_one(5, {"mode": "agent", "prompt": s.cfg["memory"]["save_prompt"], "text": "", "images": [], "save": True}))
    assert "Jmenuji se Honza" in seen[0][0] and s.last_mode[5] == "chat"


def _svc_with(monkeypatch, **bot):
    cfg = settings.load_config()
    b = {**cfg["bots"]["qwen"], **bot}
    s = BotService("qwen", b, "t", Auth([1], "JBSWY3DPEHPK3PXP"), cfg["general"], qwen=None)
    s.app = SimpleNamespace(bot=FakeBot())

    async def fake_complete(url, messages, **kw):
        s.last_messages = messages
        return "Ok."
    monkeypatch.setattr(chatmod, "complete", fake_complete)
    return s


def test_memory_disabled_no_summary_no_save(tmp_path, monkeypatch):
    (tmp_path / "README.md").write_text("SECRET-NOTE")
    mem = {**settings.load_config()["bots"]["qwen"]["memory"], "enabled": False, "dir": str(tmp_path)}
    s = _svc_with(monkeypatch, memory=mem)
    asyncio.run(s.run_chat(5, {"mode": "chat", "text": "Zapamatuj si, že bydlím v Brně", "prompt": "x", "images": [], "uid": 5}))
    assert "SECRET-NOTE" not in s.last_messages[0]["content"]
    assert s.queue.qsize() == 0 and "Saving" not in s.app.bot.sent[0]


def test_memory_enabled_adds_summary_and_custom_notice(tmp_path, monkeypatch):
    (tmp_path / "README.md").write_text("SECRET-NOTE")
    mem = {**settings.load_config()["bots"]["qwen"]["memory"], "dir": str(tmp_path), "notice_text": "NOTICE-X"}
    s = _svc_with(monkeypatch, memory=mem)
    asyncio.run(s.run_chat(5, {"mode": "chat", "text": "my name is Jan", "prompt": "x", "images": [], "uid": 5}))
    assert "SECRET-NOTE" in s.last_messages[0]["content"]
    assert "NOTICE-X" in s.app.bot.sent[0] and s.queue.qsize() == 1


def test_memory_enabled_without_save_prompt_does_not_save(tmp_path, monkeypatch):
    mem = {"enabled": True, "dir": str(tmp_path), "save_prompt": "", "notice_text": "N"}
    s = _svc_with(monkeypatch, memory=mem)
    asyncio.run(s.run_chat(5, {"mode": "chat", "text": "my name is Jan", "prompt": "x", "images": [], "uid": 5}))
    assert s.queue.qsize() == 0


def test_custom_triggers_replace_builtin_lists():
    assert chatmod.wants_agent("spusť to")
    trig = chatmod.compile_triggers([r"\bzzz\b"])
    assert not chatmod.wants_agent("spusť to", trig) and chatmod.wants_agent("zzz now", trig)
    assert chatmod.route("zzz", "auto", agent_triggers=trig)[0] == "agent"
    assert chatmod.compile_triggers([]) is None
    save = chatmod.compile_triggers([r"\bnote\s+it\b"])
    assert chatmod.wants_save("please note it", save) and not chatmod.wants_save("my name is Jan", save)
