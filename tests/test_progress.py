"""Task 8: subagent progress lines."""
import asyncio
import json
from pathlib import Path

from telegram_bridge import progress
from telegram_bridge.runner import parse_event, parse_events
from test_frontend import make, script_run, run_task

FIX = Path(__file__).parent / "fixtures" / "stream_subagent.jsonl"


def fixture_events():
    out = []
    for line in FIX.read_text(encoding="utf-8").splitlines():
        out += parse_events(line)
    return out


def test_fixture_flow():
    evs = fixture_events()
    starts = [e for e in evs if e["kind"] == "subagent_start"]
    assert len(starts) == 1 and starts[0]["description"] == "List files in folder"
    assert starts[0]["model"] == "haiku" and starts[0]["agent_type"] == "general-purpose"
    sub_tools = [e for e in evs if e["kind"] == "tool" and e["parent"] == starts[0]["id"]]
    assert len(sub_tools) >= 1 and sub_tools[0]["sub"]
    ends = [e for e in evs if e["kind"] == "subagent_end" and e["id"] == starts[0]["id"]]
    assert len(ends) == 1
    t = progress.Tracker(lambda: 100.0)
    seen = []
    for e in evs:
        t.feed(e)
        seen.append(list(t.lines(103.0)))
    assert any(seen) and any(l.startswith("↳ List files in folder · haiku · 0:03 · 🔧 PowerShell") for s in seen for l in s)
    assert t.lines(110.0) == [] and t.done == 1


def test_parallel_subagents_in_one_line():
    line = json.dumps({"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
        {"type": "tool_use", "id": "a1", "name": "Agent", "input": {"description": "one", "subagent_type": "x"}},
        {"type": "tool_use", "id": "a2", "name": "Task", "input": {"description": "two", "model": "opus"}}]}})
    evs = parse_events(line)
    assert evs[0]["kind"] == "tool" and parse_event(line) == evs[0]
    assert [e["id"] for e in evs if e["kind"] == "subagent_start"] == ["a1", "a2"]
    t = progress.Tracker(lambda: 0.0)
    for e in evs:
        t.feed(e)
    lines = t.lines(65)
    assert lines[0].startswith("↳ one · x · 1:05") and lines[1].startswith("↳ two · opus")
    assert t.feed({"kind": "subagent_end", "id": "a1"}) and t.done == 1 and len(t.lines(65)) == 1


def test_unknown_parent_and_unknown_end_ignored():
    t = progress.Tracker(lambda: 0.0)
    assert not t.feed({"kind": "tool", "name": "Bash", "detail": "ls", "parent": "nope"})
    assert not t.feed({"kind": "tool", "name": "Bash", "detail": "ls", "parent": None})
    assert not t.feed({"kind": "subagent_end", "id": "nope"})
    assert t.done == 0 and t.lines(5) == []


def test_more_than_five_collapsed():
    t = progress.Tracker(lambda: 0.0)
    for i in range(7):
        t.feed({"kind": "subagent_start", "id": str(i), "description": "d" * 60, "model": "m", "agent_type": None})
    lines = t.lines(1)
    assert len(lines) == 6 and lines[-1] == "+2 more" and lines[0].startswith("↳ " + "d" * 40 + " · m")


def test_user_event_without_tool_result():
    line = json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}})
    assert parse_event(line) is None and parse_events(line) == []
    assert parse_events("not json") == [] and parse_events(json.dumps({"type": "user", "message": "x"})) == []


def start(i="a1", desc="job", model="haiku"):
    return {"kind": "subagent_start", "id": i, "description": desc, "model": model, "agent_type": None}


def test_status_text_shows_lines_and_done():
    s = make()
    txt = s.status_text(5, 2, "🔧 Bash: ls", ["↳ job · haiku · 0:01"], 2)
    assert txt.splitlines() == ["⏳ Claude is working… 5s · step 2", "🔧 Bash: ls", "↳ job · haiku · 0:01", "✓ 2 done"]
    assert s.status_text(5, 0, "") == "⏳ Claude is working… 5s"


import pytest


@pytest.mark.parametrize("flag", ["stopped", "timed_out"])
def test_missing_end_status_deleted_no_exception(flag):
    s = make(edit_interval_seconds=0)
    script_run(s, on_events=[start(), {"kind": "tool", "name": "Bash", "detail": "ls", "sub": True, "parent": "a1"}])
    setattr(s.runner, flag, True)
    bot = s.app.bot
    statuses, orig = [], bot.send_message

    async def spy(chat, text, **kw):
        m = await orig(chat, text, **kw)
        statuses.append(m)
        return m
    bot.send_message = spy
    run_task(s)
    assert any("↳ job · haiku" in e for e in bot.edits)   # the line was shown while running
    assert statuses[0].deleted                                      # status message gone despite no tool_result
    n = len(bot.edits)
    asyncio.run(asyncio.sleep(0))
    assert len(bot.edits) == n                                      # nothing edits it afterwards


def test_status_lines_redacted_and_done_counted():
    s = make(edit_interval_seconds=0)
    secret = "sk-ant-api03-" + "A" * 40
    script_run(s, on_events=[start(desc="use " + secret), {"kind": "subagent_end", "id": "a1"}])
    bot = run_task(s)
    assert not any(secret in e for e in bot.edits)
    assert any("✓ 1 done" in e for e in bot.edits)
