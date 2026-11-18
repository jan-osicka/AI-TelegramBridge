"""Context tracking: stream-json parsing (shapes verified against the real CLI), stats, footer, warnings, state."""
import asyncio
import json
import sys
from pathlib import Path

from telegram_bridge import ctxinfo
from telegram_bridge import settings
from telegram_bridge.runner import Runner, RunStats, context_tokens, parse_event
from telegram_bridge.state import StateStore

PY = sys.executable

# Shapes recorded from `claude -p --output-format stream-json --verbose --model haiku` (trimmed).
ASSISTANT = {"type": "assistant", "message": {
    "model": "claude-haiku-5-5", "role": "assistant", "content": [{"type": "text", "text": "Hi there, friend!"}],
    "usage": {"input_tokens": 2, "cache_creation_input_tokens": 8800, "cache_read_input_tokens": 19550,
              "output_tokens": 5}}, "parent_tool_use_id": None, "session_id": "s1"}
RESULT = {"type": "result", "subtype": "success", "is_error": False, "result": "Hi there, friend!",
          "session_id": "s1", "total_cost_usd": 0.0019607,
          "usage": {"input_tokens": 2, "cache_creation_input_tokens": 8800, "cache_read_input_tokens": 19550},
          "modelUsage": {"claude-haiku-5-5": {"inputTokens": 2, "contextWindow": 1000000, "costUSD": 0.0019607}}}


def line(d):
    return json.dumps(d)


def test_context_tokens_sums_input_and_cache():
    assert context_tokens(ASSISTANT["message"]["usage"]) == 2 + 8800 + 19550
    assert context_tokens({"output_tokens": 9}) is None
    assert context_tokens(None) is None and context_tokens("x") is None
    assert context_tokens({"input_tokens": None, "cache_read_input_tokens": "5"}) is None


def test_parse_assistant_text_carries_usage():
    ev = parse_event(line(ASSISTANT))
    assert ev["kind"] == "text" and ev["text"] == "Hi there, friend!"
    assert ev["ctx"] == 28352 and ev["model"] == "claude-haiku-5-5" and ev["sub"] is False


def test_parse_tool_and_thinking_only_and_subagent():
    tool = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}],
                                             "usage": {"input_tokens": 1, "cache_read_input_tokens": 99}}}
    ev = parse_event(line(tool))
    assert ev["kind"] == "tool" and ev["name"] == "Bash" and ev["ctx"] == 100
    think = {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "..."}],
                                              "usage": {"input_tokens": 7}}}
    assert parse_event(line(think))["kind"] == "usage"
    assert parse_event(line({**think, "message": {"content": [{"type": "thinking"}]}})) is None
    sub = {**ASSISTANT, "parent_tool_use_id": "toolu_1"}
    assert parse_event(line(sub))["sub"] is True


def test_parse_result_cost_and_windows():
    ev = parse_event(line(RESULT))
    assert ev["kind"] == "result" and ev["cost_usd"] == 0.0019607
    assert ev["windows"] == {"claude-haiku-5-5": 1000000}
    bare = parse_event(line({"type": "result", "result": "x", "session_id": "s"}))
    assert bare["cost_usd"] is None and bare["windows"] == {}
    weird = parse_event(line({"type": "result", "modelUsage": {"m": "bad", "n": {"contextWindow": "x"}}, "total_cost_usd": "1"}))
    assert weird["windows"] == {} and weird["cost_usd"] is None


def test_parse_compact_boundary_and_other_system_events():
    ev = parse_event(line({"type": "system", "subtype": "compact_boundary", "session_id": "s",
                           "compact_metadata": {"trigger": "auto", "pre_tokens": 180000}}))
    assert ev == {"kind": "compact", "pre_tokens": 180000}
    assert parse_event(line({"type": "system", "subtype": "compact_boundary"}))["kind"] == "compact"
    assert parse_event(line({"type": "system", "subtype": "init", "session_id": "s"}))["kind"] == "session"


def fake_runner(events, cfg=None):
    code = "import sys;[print(l,flush=True) for l in " + repr([line(e) for e in events]) + "]"
    return Runner({"command": PY, "args": ["-c", code], **(cfg or {})}, ".")


def test_runner_stats_use_last_main_message_and_model_window():
    big = {**ASSISTANT, "message": {**ASSISTANT["message"], "usage": {"input_tokens": 1, "cache_read_input_tokens": 49999}}}
    sub = {**ASSISTANT, "parent_tool_use_id": "t", "message": {**ASSISTANT["message"],
                                                                "usage": {"input_tokens": 900000}}}
    events = [ASSISTANT, sub, big, {"type": "system", "subtype": "compact_boundary"}, RESULT]
    r = fake_runner(events, {"context_window": 200000})
    final, sid, err = asyncio.run(r.run("x"))
    assert (final, sid, err) == ("Hi there, friend!", "s1", False)
    assert r.stats == RunStats(50000, 1000000, 0.0019607, True, "claude-haiku-5-5")


def test_runner_window_falls_back_to_config_and_source_config_wins():
    r = fake_runner([ASSISTANT, {**RESULT, "modelUsage": {}}], {"context_window": 200000})
    asyncio.run(r.run("x"))
    assert r.stats.context_window == 200000 and r.stats.compacted is False
    r = fake_runner([ASSISTANT, RESULT], {"context_window": 131072, "context_window_source": "config"})
    asyncio.run(r.run("x"))
    assert r.stats.context_window == 131072
    r = fake_runner([ASSISTANT, RESULT])
    asyncio.run(r.run("x"))
    assert r.stats.context_window == 1000000      # no config value: the reported one is used


def test_runner_stats_reset_each_run_and_zero_cost_dropped():
    r = fake_runner([{**RESULT, "total_cost_usd": 0}])
    asyncio.run(r.run("x"))
    assert r.stats.context_tokens is None and r.stats.cost_usd is None


def test_config_has_context_windows():
    b = settings.load_config()["bots"]
    assert b["claude"]["context_window"] == 200000
    assert b["qwen"]["context_window"] == 131072 and b["qwen"]["context_window_source"] == "config"


# ---- ctxinfo
def test_percent_and_footer():
    assert ctxinfo.percent(84000, 200000) == 42 and ctxinfo.percent(None, 200000) is None
    assert ctxinfo.percent(5, 0) is None
    assert ctxinfo.footer(42) == "· 42% context"
    assert ctxinfo.footer(None) == ""


def test_warnings_fire_once_per_level():
    w = set()
    assert ctxinfo.warnings(50, w) == [] and w == set()
    out = ctxinfo.warnings(73, w)
    assert len(out) == 1 and out[0].startswith("⚠️ Context 73% full. Claude will auto-compact soon") and "/exit" in out[0]
    assert ctxinfo.warnings(75, w) == []
    out = ctxinfo.warnings(91, w)
    assert len(out) == 1 and out[0].startswith("🚨 Context 91% full") and "/exit" in out[0]
    assert ctxinfo.warnings(99, w) == [] and w == {70, 90}
    w2 = set()
    assert len(ctxinfo.warnings(95, w2)) == 1 and w2 == {70, 90}      # jumping to 95 sends only the strong one
    assert ctxinfo.warnings(None, set()) == []


def test_large_session_notice_numbers():
    s = ctxinfo.large_session_notice(46000)
    assert "~46k tokens" in s and "~20 s" in s and "/exit" in s


# ---- state
def test_state_persists_ctx(tmp_path):
    st = StateStore(tmp_path / "s.json")
    ctx = {5: {"tokens": 84000, "window": 200000, "pct": 42, "warned": [70], "trimmed": True}}
    st.save("claude", {5: "sid"}, {}, {}, ctx)
    assert st.load("claude")["ctx"] == ctx
    st.save("claude", {5: "sid"}, {}, {})          # ctx omitted = empty
    assert st.load("claude")["ctx"] == {}
    (tmp_path / "s.json").write_text(json.dumps({"bots": {"claude": {"ctx": {"5": {"pct": "x", "warned": ["a"]}}}}}))
    assert st.load("claude")["ctx"] == {5: {"tokens": None, "window": None, "pct": None, "warned": [], "trimmed": False}}
