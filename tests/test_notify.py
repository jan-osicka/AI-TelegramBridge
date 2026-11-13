import asyncio
import json

import pytest
from telegram.error import BadRequest, InvalidToken

from telegram_bridge import cli, notify, redaction, settings

TOKEN = "123456789:AAFakeTokenValueForTestsOnly_abcdefghij"


class FBot:
    def __init__(self, fail=None):
        self.sent, self.docs, self.fail = [], [], fail

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send_message(self, chat, text, **kw):
        if self.fail:
            raise self.fail
        if kw.get("parse_mode") == "HTML" and "BAD" in text:
            raise BadRequest("Can't parse entities")
        self.sent.append((chat, text, kw))

    async def send_document(self, chat, f, filename=None, **kw):
        self.docs.append((chat, f.read(), filename))


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / "outbox").mkdir(parents=True)
    (h / "logs").mkdir()
    monkeypatch.setenv("TELEGRAM_BRIDGE_HOME", str(h))
    for name, sub in (("STATE_DIR", ""), ("OUTBOX", "outbox"), ("LOGS", "logs")):
        monkeypatch.setattr(settings, name, h / sub if sub else h)
    (h / "config.toml").write_text(
        '[general]\ncwd = "."\n[bots.alpha]\ntoken_env = "ALPHA_TOKEN"\n[bots.beta]\ntoken_env = "BETA_TOKEN"\n')
    (h / ".env").write_text(f"ALPHA_TOKEN={TOKEN}\nALLOWED_USER_IDS=11,22\n")
    monkeypatch.setattr(settings, "config_path", lambda: h / "config.toml")
    monkeypatch.setattr(redaction, "_known", [])
    return h


def run(args, bot, monkeypatch, stdin=None):
    monkeypatch.setattr(notify, "bot_factory", lambda token: bot)
    if stdin is not None:
        import io
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    return cli.main(["notify", *args])


def test_sends_to_each_id(home, monkeypatch):
    b = FBot()
    assert run(["hello **x**"], b, monkeypatch) == 0
    assert [s[0] for s in b.sent] == [11, 22]
    assert "<b>x</b>" in b.sent[0][1]


def test_stdin_and_silent(home, monkeypatch):
    b = FBot()
    assert run(["--silent"], b, monkeypatch, stdin="from stdin") == 0
    assert "from stdin" in b.sent[0][1] and b.sent[0][2]["disable_notification"] is True


def test_long_text_split(home, monkeypatch):
    b = FBot()
    assert run(["word " * 2000], b, monkeypatch) == 0
    assert len(b.sent) > 2 and all(len(s[1]) <= 4096 for s in b.sent)


def test_redaction(home, monkeypatch):
    b = FBot()
    assert run(["key sk-ant-abcdefghijklmnopqrstuvwxyz0123456789 and " + TOKEN], b, monkeypatch) == 0
    assert "sk-ant" not in b.sent[0][1] and TOKEN not in b.sent[0][1]


def test_html_fallback_plain(home, monkeypatch):
    b = FBot()
    assert run(["BAD <text"], b, monkeypatch) == 0
    assert "parse_mode" not in b.sent[0][2]


def test_file_in_outbox_sent(home, monkeypatch):
    f = home / "outbox" / "a.txt"
    f.write_text("data")
    b = FBot()
    assert run(["--file", str(f), "hi"], b, monkeypatch) == 0
    assert [d[0] for d in b.docs] == [11, 22] and b.docs[0][1] == b"data"


def test_relative_file_uses_cwd(home, monkeypatch):
    (home / "outbox" / "r.txt").write_text("x")
    monkeypatch.chdir(home / "outbox")
    b = FBot()
    assert run(["--file", "r.txt", "hi"], b, monkeypatch) == 0
    assert len(b.docs) == 2


def test_denied_file_exit5_text_sent(home, monkeypatch, capsys):
    b = FBot()
    assert run(["--file", str(home / ".env"), "hi"], b, monkeypatch) == 5
    assert b.sent and not b.docs
    err = capsys.readouterr().err
    assert ".env" in err and TOKEN not in err and "\n" == err[-1]


def test_invalid_token_exit4_no_token(home, monkeypatch, capsys):
    b = FBot(fail=InvalidToken(f"bad {TOKEN}"))
    assert run(["hi"], b, monkeypatch) == 4
    err = capsys.readouterr().err
    assert TOKEN not in err and len(err.strip().splitlines()) == 1


def test_network_error_exit4(home, monkeypatch, capsys):
    from telegram.error import NetworkError
    assert run(["hi"], FBot(fail=NetworkError("down")), monkeypatch) == 4


def test_empty_exit2(home, monkeypatch):
    assert run([], FBot(), monkeypatch, stdin="  \n") == 2


def test_unknown_bot_and_missing_token_exit2(home, monkeypatch):
    assert run(["--bot", "nope", "hi"], FBot(), monkeypatch) == 2
    assert run(["--bot", "beta", "hi"], FBot(), monkeypatch) == 2


def test_missing_ids_exit2(home, monkeypatch):
    (home / ".env").write_text(f"ALPHA_TOKEN={TOKEN}\n")
    assert run(["hi"], FBot(), monkeypatch) == 2


def test_works_when_locked(home, monkeypatch):
    (home / "state.json").write_text(json.dumps({"locked": True}))
    assert run(["hi"], FBot(), monkeypatch) == 0


def test_log_has_no_text_or_token(home, monkeypatch):
    run(["secret message body"], FBot(), monkeypatch)
    log = (home / "logs" / "notify.log").read_text()
    assert "secret message body" not in log and TOKEN not in log
    assert "11,22" in log and "len=19" in log


def test_stdin_utf8_bytes_and_bom(home, monkeypatch):
    import io
    import types
    text = "Příliš žluťoučký kůň 🐴"
    fake = types.SimpleNamespace(buffer=io.BytesIO(b"\xef\xbb\xbf" + text.encode("utf-8")),
                                 isatty=lambda: False)
    monkeypatch.setattr("sys.stdin", fake)
    b = FBot()
    monkeypatch.setattr(notify, "bot_factory", lambda token: b)
    assert cli.main(["notify"]) == 0
    assert b.sent[0][1] == text


def test_stderr_cp1252_safe(home, monkeypatch):
    import io
    err = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
    monkeypatch.setattr("sys.stderr", err)
    assert run(["--file", str(home / "outbox" / "žluť🐴.txt"), "hi"], FBot(), monkeypatch) == 5
    err.flush()


def test_refused_notes_accumulate_across_ids(home, monkeypatch, capsys):
    class B(FBot):
        async def send_document(self, chat, f, filename=None, **kw):
            if chat == 11:
                raise OSError("x")
    f = home / "outbox" / "a.txt"
    f.write_text("d")
    assert run(["--file", str(f), "hi"], B(), monkeypatch) == 5
    assert "upload failed" in capsys.readouterr().err


def test_unexpected_error_exit4_and_logged(home, monkeypatch, capsys):
    class B(FBot):
        async def send_message(self, *a, **k):
            raise RuntimeError(f"boom {TOKEN}")
    assert run(["hi"], B(), monkeypatch) == 4
    err = capsys.readouterr().err
    assert TOKEN not in err and len(err.strip().splitlines()) == 1
    assert "exit=4" in (home / "logs" / "notify.log").read_text()


def test_no_stdin_no_stderr(home, monkeypatch):
    monkeypatch.setattr("sys.stdin", None)
    monkeypatch.setattr("sys.stderr", None)
    monkeypatch.setattr(notify, "bot_factory", lambda token: FBot())
    assert cli.main(["notify"]) == 2   # nothing to send, and no crash
