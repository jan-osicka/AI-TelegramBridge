import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_bridge import outfiles, settings
from telegram_bridge.runner import build_cmd
from test_frontend import FakeBot, make, script_run


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / "outbox").mkdir(parents=True)
    monkeypatch.setenv("TELEGRAM_BRIDGE_HOME", str(h))
    monkeypatch.setattr(settings, "OUTBOX", h / "outbox")
    return h


def test_extract_basic_quoted_caption():
    text = 'Done\n[tg-file] C:/a/b.txt\n[tg-file] "C:/my dir/r.pdf" | the report\nbye'
    out, items = outfiles.extract(text)
    assert out == "Done\nbye"
    assert items == [("C:/a/b.txt", None), ("C:/my dir/r.pdf", "the report")]


def test_extract_max_ten_extras_reported():
    text = "hi\n" + "\n".join(f"[tg-file] /o/f{i}.txt" for i in range(11))
    out, items = outfiles.extract(text)
    assert len(items) == 10
    assert "[tg-file]" not in out
    assert "📎 not sent: f10.txt (over the limit of 10 files)" in out


def test_check_inside_outbox_ok(home):
    f = home / "outbox" / "a.txt"
    f.write_text("x")
    assert outfiles.check(str(f), [home / "outbox"], []) == (f.resolve(), None)


def test_check_env_via_dotdot(home):
    (home / ".env").write_text("T=1")
    p, why = outfiles.check(str(home / "outbox" / ".." / ".env"), [home / "outbox"], [])
    assert p is None and why == "blocked file"


def test_check_traversal_outside_allowed(home, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (tmp_path / "secret.txt").write_text("x")
    p, why = outfiles.check(str(allowed / ".." / "secret.txt"), [allowed], [])
    assert (p, why) == (None, "outside allowed folders")
    p, why = outfiles.check(str(allowed / ".." / ".." / "x"), [allowed], [])
    assert p is None


@pytest.mark.skipif(sys.platform != "win32", reason="case-insensitive paths are Windows-only")
def test_check_home_case_variant(home):
    (home / "state.json").write_text("{}")
    p, why = outfiles.check(str(home).upper() + "/STATE.JSON", [home], [])
    assert (p, why) == (None, "blocked folder")


def test_check_symlink_to_env(home, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (home / ".env").write_text("T=1")
    link = allowed / "innocent.txt"
    try:
        os.symlink(home / ".env", link)
    except OSError as e:
        pytest.skip(f"symlinks not permitted here: {e}")
    p, why = outfiles.check(str(link), [allowed], [])
    assert p is None and why in ("blocked file", "blocked folder")


def test_check_git_and_env_names(home, tmp_path):
    allowed = tmp_path / "allowed"
    (allowed / ".git").mkdir(parents=True)
    (allowed / ".git" / "config").write_text("x")
    (allowed / ".env.local").write_text("x")
    assert outfiles.check(str(allowed / ".git" / "config"), [allowed], [])[1] == "blocked folder"
    assert outfiles.check(str(allowed / ".env.local"), [allowed], [])[1] == "blocked file"


def test_check_denied_wins_and_code_dir(home, tmp_path):
    allowed = tmp_path / "allowed"
    (allowed / "priv").mkdir(parents=True)
    f = allowed / "priv" / "a.txt"
    f.write_text("x")
    assert outfiles.check(str(f), [allowed], [allowed / "priv"])[1] == "blocked folder"
    assert outfiles.check(str(settings.CODE_DIR / "pyproject.toml"), [settings.CODE_DIR], [])[1] == "blocked folder"


def test_check_too_large_and_missing(home):
    big = home / "outbox" / "big.bin"
    with open(big, "wb") as f:
        f.truncate(51 * 1024 * 1024)
    assert outfiles.check(str(big), [home / "outbox"], [])[1] == "too large (>50 MB)"
    assert outfiles.check(str(home / "outbox" / "nope.txt"), [home / "outbox"], [])[1] == "not found"


def test_kind(tmp_path):
    for name, k in (("a.jpg", "photo"), ("a.PNG", "photo"), ("a.webp", "photo"), ("a.mp4", "video"),
                    ("a.mov", "video"), ("a.pdf", "document"), ("a", "document")):
        (tmp_path / name).write_text("x")
        assert outfiles.kind(tmp_path / name) == k
    big = tmp_path / "big.jpg"
    with open(big, "wb") as f:
        f.truncate(11 * 1024 * 1024)
    assert outfiles.kind(big) == "document"


class Bot(FakeBot):
    def __init__(self, fail=False):
        super().__init__()
        self.files, self.fail = [], fail

    async def send_document(self, chat, f, filename=None, **kw):
        if self.fail:
            raise ConnectionError("net down secret-url")
        self.files.append(("doc", chat, f.read(), filename, kw))

    async def send_photo(self, chat, f, **kw):
        self.files.append(("photo", chat, f.read(), None, kw))


def test_send_redacts_caption_and_notes_upload_failure(home):
    f = home / "outbox" / "a.txt"
    f.write_text("hello")
    bot = Bot()
    notes = asyncio.run(outfiles.send(bot, 5, [(str(f), "token=abcdef123456XYZ")], [home / "outbox"], [], 9))
    assert notes == [] and bot.files[0][3] == "a.txt" and "abcdef123456XYZ" not in bot.files[0][4]["caption"]
    assert bot.files[0][4]["reply_parameters"].message_id == 9
    notes = asyncio.run(outfiles.send(Bot(fail=True), 5, [(str(f), None)], [home / "outbox"], []))
    assert notes == ["📎 not sent: a.txt (upload failed)"]


def test_run_task_sends_file_after_answer(home):
    f = home / "outbox" / "a.txt"
    f.write_text("data")
    s = make()
    s.app.bot = Bot()
    script_run(s, f"Done\n[tg-file] {f}")
    asyncio.run(s.run_task(5, "hi", 77))
    bot = s.app.bot
    assert bot.sent[1]["text"] == "Done"
    assert bot.files[0][0] == "doc" and bot.files[0][2] == b"data" and bot.files[0][3] == "a.txt"
    assert bot.files[0][4]["reply_parameters"].message_id == 77


def test_run_task_denied_path_note(home):
    (home / ".env").write_text("T=1")
    s = make()
    s.app.bot = Bot()
    script_run(s, f"Done\n[tg-file] {home / '.env'}")
    asyncio.run(s.run_task(5, "hi", 77))
    assert s.app.bot.files == []
    assert s.app.bot.sent[1]["text"] == "Done\n📎 not sent: .env (blocked file)"


def test_convention_sentence_once_and_toggle(home):
    s = make()
    cmd = build_cmd(s.cfg, "hi", "sid")
    prompt = cmd[cmd.index("--append-system-prompt") + 1]
    assert prompt.count("[tg-file]") == 1 and str(home / "outbox") in prompt
    assert build_cmd(s.cfg, "hi") == build_cmd(s.cfg, "hi")
    s2 = make(outgoing_files=False)
    cmd2 = build_cmd(s2.cfg, "hi")
    assert "--append-system-prompt" not in cmd2 or "[tg-file]" not in cmd2[cmd2.index("--append-system-prompt") + 1]


def test_extract_ignores_markers_in_fences():
    text = "a\n```\n[tg-file] /x/y.txt\n```\n~~~\n[tg-file] /z\n~~~\n[tg-file] /real.txt"
    out, items = outfiles.extract(text)
    assert items == [("/real.txt", None)]
    assert "[tg-file] /x/y.txt" in out and "[tg-file] /z" in out


def test_check_relative_path_uses_base(home, tmp_path):
    base = home / "outbox" / "work"
    base.mkdir()
    (base / "f.txt").write_text("x")
    p, why = outfiles.check("f.txt", [settings.OUTBOX], [], base=base)
    assert why is None and p == (base / "f.txt").resolve()
