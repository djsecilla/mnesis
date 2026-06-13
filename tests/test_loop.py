"""Tests for the bounded tool-use agent loop.

All tests run offline: StubProvider scripts the LLM turns; FakeToolSource
provides deterministic tool results; no network or Mnesis process needed.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from mnesis_agent.fake_tools import FakeToolSource
from mnesis_agent.loop import (
    AgentProfile,
    AgentResult,
    ThoughtStep,
    ToolStep,
    _extract_citations,
    run_agent,
)
from mnesis_agent.mcp_client import MCPToolError, ToolSpec
from mnesis_agent.provider import AssistantTurn, StubProvider, ToolCall
from mnesis_agent.registry import ToolRegistry


def run(coro):
    return asyncio.run(coro)


# ── Fixtures ──────────────────────────────────────────────────────────────────

TOOLS = [
    ToolSpec("mnesis_query", "Search", {"type": "object", "properties": {"query": {"type": "string"}}}),
    ToolSpec("mnesis_ingest", "Ingest", {"type": "object", "properties": {"text": {"type": "string"}}}),
]


def _make(
    script: list[AssistantTurn],
    *,
    extra_responses: dict | None = None,
    profile_kwargs: dict | None = None,
    write_tool_names: set[str] | None = None,
    audit_hook=None,
) -> AgentResult:
    """Build the standard rig and run the loop."""
    source = FakeToolSource(responses=extra_responses)
    registry = ToolRegistry()
    registry.add_source(source)
    provider = StubProvider(script=script)
    profile = AgentProfile(system="You are a helpful agent.", **(profile_kwargs or {}))
    return run(run_agent(
        profile, "What uses Redis?", TOOLS, provider, registry,
        write_tool_names=write_tool_names,
        audit_hook=audit_hook,
    ))


def _turn(text="", calls: list[ToolCall] | None = None, reason="tool_use", usage=None):
    """Shorthand for an AssistantTurn."""
    return AssistantTurn(
        text=text,
        tool_calls=calls or [],
        stop_reason=reason,
        usage=usage or {"input_tokens": 10, "output_tokens": 5},
    )


def _call(name, args=None, *, idx=0):
    return ToolCall(id=f"tc_{idx}", name=name, args=args or {})


def _final(text="Atlas uses Redis for caching."):
    return _turn(text=text, calls=[], reason="end_turn")


# ── Normal completion ─────────────────────────────────────────────────────────


def test_normal_single_tool_call():
    result = _make([
        _turn(calls=[_call("mnesis_query", {"query": "redis"})]),
        _final(),
    ])
    assert result.stop_reason == "end_turn"
    assert result.final_text == "Atlas uses Redis for caching."
    assert result.iterations == 1


def test_multi_step_completion():
    result = _make([
        _turn(calls=[_call("mnesis_query", {"query": "redis"}, idx=0)]),
        _turn(calls=[_call("mnesis_query", {"query": "atlas"}, idx=1)]),
        _final("Done with two searches."),
    ])
    assert result.stop_reason == "end_turn"
    assert result.final_text == "Done with two searches."
    assert result.iterations == 2


def test_direct_answer_no_tools():
    """Model gives a direct answer without calling any tools."""
    result = _make([_final("The answer is 42.")])
    assert result.stop_reason == "end_turn"
    assert result.final_text == "The answer is 42."
    assert result.tools_used == []
    assert result.iterations == 0


# ── Transcript ────────────────────────────────────────────────────────────────


def test_transcript_contains_thought_and_tool_steps():
    result = _make([
        _turn(calls=[_call("mnesis_query", {"query": "redis"})]),
        _final(),
    ])
    kinds = [s.kind for s in result.transcript]
    assert "thought" in kinds
    assert "tool" in kinds


def test_transcript_thought_step_shape():
    result = _make([
        _turn(text="Searching…", calls=[_call("mnesis_query", {"query": "x"})]),
        _final(),
    ])
    thoughts = [s for s in result.transcript if isinstance(s, ThoughtStep)]
    assert thoughts[0].text == "Searching…"
    assert len(thoughts[0].tool_calls) == 1


def test_transcript_tool_step_shape():
    result = _make([
        _turn(calls=[_call("mnesis_query", {"query": "redis"})]),
        _final(),
    ])
    tool_steps = [s for s in result.transcript if isinstance(s, ToolStep)]
    assert len(tool_steps) == 1
    ts = tool_steps[0]
    assert ts.tool_name == "mnesis_query"
    assert ts.call_id == "tc_0"
    assert ts.args == {"query": "redis"}
    assert ts.is_error is False
    assert ts.result  # non-empty (canned FakeToolSource response)


def test_transcript_multiple_rounds_ordered():
    result = _make([
        _turn(calls=[_call("mnesis_query", {"query": "a"}, idx=0)]),
        _turn(calls=[_call("mnesis_query", {"query": "b"}, idx=1)]),
        _final(),
    ])
    kinds = [s.kind for s in result.transcript]
    # thought, tool, thought, tool, thought(final)
    assert kinds == ["thought", "tool", "thought", "tool", "thought"]


def test_transcript_all_turns_share_consistent_turn_numbers():
    result = _make([
        _turn(calls=[_call("mnesis_query", {"query": "x"}, idx=0)]),
        _turn(calls=[_call("mnesis_query", {"query": "y"}, idx=1)]),
        _final(),
    ])
    thoughts = [s for s in result.transcript if isinstance(s, ThoughtStep)]
    tools = [s for s in result.transcript if isinstance(s, ToolStep)]
    # Turn numbers are monotonically non-decreasing
    all_turns = [s.turn for s in result.transcript]
    assert all_turns == sorted(all_turns)
    # Each ThoughtStep's turn matches the ToolSteps that follow it in the same round
    assert thoughts[0].turn == tools[0].turn
    assert thoughts[1].turn == tools[1].turn


# ── tools_used ────────────────────────────────────────────────────────────────


def test_tools_used_distinct_and_sorted():
    result = _make([
        _turn(calls=[
            _call("mnesis_query", {"query": "a"}, idx=0),
            _call("mnesis_ingest", {"text": "x"}, idx=1),
        ]),
        _final(),
        ], extra_responses={"mnesis_ingest": '{"action_taken":"new","page_id":"p1","redaction_count":0}'})
    assert result.tools_used == sorted(result.tools_used)
    assert "mnesis_query" in result.tools_used
    assert "mnesis_ingest" in result.tools_used
    # Distinct (even if called multiple times)
    assert len(result.tools_used) == len(set(result.tools_used))


def test_tools_used_empty_when_no_tools_called():
    result = _make([_final()])
    assert result.tools_used == []


# ── citations ─────────────────────────────────────────────────────────────────


def test_citations_extracted_from_query_results():
    result = _make([
        _turn(calls=[_call("mnesis_query", {"query": "redis"})]),
        _final(),
    ])
    # FakeToolSource returns {"hits": [{"id": "atlas", ...}]}
    assert "atlas" in result.citations


def test_citations_unique_no_duplicates():
    result = _make([
        _turn(calls=[
            _call("mnesis_query", {"query": "redis"}, idx=0),
            _call("mnesis_query", {"query": "cache"}, idx=1),  # same canned result → same id
        ]),
        _final(),
    ])
    assert result.citations.count("atlas") == 1


def test_citations_empty_when_no_hits():
    custom_resp = {"mnesis_query": '{"hits": []}'}
    result = _make([
        _turn(calls=[_call("mnesis_query", {"query": "nothing"})]),
        _final(),
    ], extra_responses=custom_resp)
    assert result.citations == []


def test_citation_extractor_disabled_with_none():
    result = _make([
        _turn(calls=[_call("mnesis_query", {"query": "redis"})]),
        _final(),
    ])
    # Override run_agent directly to pass citation_extractor=None
    source = FakeToolSource()
    registry = ToolRegistry()
    registry.add_source(source)
    provider = StubProvider(script=[
        _turn(calls=[_call("mnesis_query", {"query": "redis"})]),
        _final(),
    ])
    profile = AgentProfile(system="sys")
    result2 = run(run_agent(
        profile, "q", TOOLS, provider, registry, citation_extractor=None
    ))
    assert result2.citations == []


# ── writes ────────────────────────────────────────────────────────────────────


def test_writes_tracked():
    result = _make([
        _turn(calls=[_call("mnesis_ingest", {"text": "some text"}, idx=0)]),
        _final(),
    ], extra_responses={"mnesis_ingest": '{"action_taken":"new","page_id":"p1","redaction_count":0}'},
    write_tool_names={"mnesis_ingest"})
    assert len(result.writes) == 1
    assert result.writes[0].name == "mnesis_ingest"


def test_writes_empty_when_no_write_tool_names():
    result = _make([
        _turn(calls=[_call("mnesis_ingest", {"text": "t"}, idx=0)]),
        _final(),
    ], extra_responses={"mnesis_ingest": '{"action_taken":"new","page_id":"p1","redaction_count":0}'},
    write_tool_names=None)
    assert result.writes == []


# ── usage ─────────────────────────────────────────────────────────────────────


def test_usage_accumulated_across_turns():
    # Two-turn script with known per-turn usage
    result = _make([
        _turn(calls=[_call("mnesis_query", {"query": "x"})], usage={"input_tokens": 100, "output_tokens": 50}),
        _final("done"),
    ])
    # 100+<final turn input> total; at minimum 100
    assert result.usage["input_tokens"] >= 100
    assert result.usage["output_tokens"] >= 50


def test_usage_zero_for_direct_answer():
    result = _make([_turn(text="done", calls=[], reason="end_turn", usage={"input_tokens": 7, "output_tokens": 3})])
    assert result.usage == {"input_tokens": 7, "output_tokens": 3}


# ── Guardrail: max_iterations ─────────────────────────────────────────────────


def test_max_iterations_stops_runaway():
    # 10-turn script of repeated tool calls (different args to avoid no_progress)
    script = [
        _turn(calls=[_call("mnesis_query", {"query": str(i)}, idx=i)])
        for i in range(10)
    ]
    result = _make(script, profile_kwargs={"max_iterations": 2})
    assert result.stop_reason == "max_iterations"
    assert result.iterations == 2


def test_max_iterations_one_allows_one_round():
    script = [
        _turn(calls=[_call("mnesis_query", {"query": "x"}, idx=0)]),
        _turn(calls=[_call("mnesis_query", {"query": "y"}, idx=1)]),
        _final(),
    ]
    result = _make(script, profile_kwargs={"max_iterations": 1})
    assert result.stop_reason == "max_iterations"
    assert result.iterations == 1


def test_max_iterations_zero_stops_immediately_on_tool_use():
    script = [_turn(calls=[_call("mnesis_query", {"query": "x"})])]
    result = _make(script, profile_kwargs={"max_iterations": 0})
    assert result.stop_reason == "max_iterations"
    assert result.iterations == 0


# ── Guardrail: max_tool_calls ─────────────────────────────────────────────────


def test_max_tool_calls_stops_loop():
    # One round with 3 tool calls, limit = 2
    script = [
        _turn(calls=[
            _call("mnesis_query", {"query": "a"}, idx=0),
            _call("mnesis_query", {"query": "b"}, idx=1),
            _call("mnesis_query", {"query": "c"}, idx=2),
        ]),
    ]
    result = _make(script, profile_kwargs={"max_tool_calls": 2})
    assert result.stop_reason == "max_tool_calls"
    # Only 2 ToolSteps recorded (3rd was blocked)
    tool_steps = [s for s in result.transcript if isinstance(s, ToolStep)]
    assert len(tool_steps) == 2


def test_max_tool_calls_across_rounds():
    # 3 rounds × 1 call each, limit = 2
    script = [
        _turn(calls=[_call("mnesis_query", {"query": str(i)}, idx=i)])
        for i in range(3)
    ] + [_final()]
    result = _make(script, profile_kwargs={"max_tool_calls": 2})
    assert result.stop_reason == "max_tool_calls"


# ── Guardrail: no_progress ────────────────────────────────────────────────────


def test_no_progress_detected_on_repeated_call():
    # Two consecutive turns calling the same tool with identical args
    script = [
        _turn(calls=[_call("mnesis_query", {"query": "redis"}, idx=0)]),
        _turn(calls=[_call("mnesis_query", {"query": "redis"}, idx=1)]),  # same sig
        _final(),
    ]
    result = _make(script, profile_kwargs={"no_progress_window": 4})
    assert result.stop_reason == "no_progress"


def test_no_progress_not_triggered_when_args_differ():
    # Same tool name but different args should NOT trigger no_progress
    script = [
        _turn(calls=[_call("mnesis_query", {"query": "redis"}, idx=0)]),
        _turn(calls=[_call("mnesis_query", {"query": "atlas"}, idx=1)]),
        _final(),
    ]
    result = _make(script, profile_kwargs={"no_progress_window": 4, "max_iterations": 5})
    assert result.stop_reason == "end_turn"


def test_no_progress_within_single_round():
    # Two identical calls in the same round
    script = [
        _turn(calls=[
            _call("mnesis_query", {"query": "redis"}, idx=0),
            _call("mnesis_query", {"query": "redis"}, idx=1),  # same sig, same round
        ]),
        _final(),
    ]
    result = _make(script, profile_kwargs={"no_progress_window": 4})
    assert result.stop_reason == "no_progress"


# ── Guardrail: deadline ───────────────────────────────────────────────────────


def test_deadline_stops_loop():
    # timeout_seconds=-1 sets a deadline 1 second in the past — fires immediately
    # on the first iteration's deadline check.
    import mnesis_agent.loop as lm
    import time as _time

    # The first monotonic() call sets the deadline = t0 + (-1) = past.
    # The second call (inside the loop) sees t1 > deadline → stop.
    # We don't need to mock; a negative timeout is already in the past.
    script = [
        _turn(calls=[_call("mnesis_query", {"query": "x"})]),
        _final(),
    ]
    result = _make(script, profile_kwargs={"timeout_seconds": -1})
    assert result.stop_reason == "deadline"


# ── Guardrail: token budget ───────────────────────────────────────────────────


def test_token_budget_stops_loop():
    # max_input_tokens=5; each turn uses 10 → exceeds on first tool-use turn
    script = [
        _turn(calls=[_call("mnesis_query", {"query": "x"})], usage={"input_tokens": 10, "output_tokens": 5}),
        _final(),
    ]
    result = _make(script, profile_kwargs={"max_input_tokens": 5})
    assert result.stop_reason == "token_budget"


# ── Tool error recovery ────────────────────────────────────────────────────────


def test_tool_error_becomes_result_and_loop_continues():
    """A tool that errors must not crash the loop — the error is fed back and
    the model can give a final answer on the next turn."""
    script = [
        _turn(calls=[_call("no_such_tool", {}, idx=0)]),  # unknown → error
        _final("Despite the error, here is my answer."),
    ]
    result = _make(script)
    assert result.stop_reason == "end_turn"
    assert "Despite the error" in result.final_text

    err_steps = [s for s in result.transcript if isinstance(s, ToolStep) and s.is_error]
    assert len(err_steps) == 1
    assert "no_such_tool" in err_steps[0].result  # error message in result


def test_tool_error_does_not_affect_tools_used():
    """Even errored tool calls are recorded in tools_used (attempt was made)."""
    script = [
        _turn(calls=[_call("no_such_tool", {})]),
        _final(),
    ]
    result = _make(script)
    assert "no_such_tool" in result.tools_used


def test_multiple_tools_one_errors_others_succeed():
    """Mixed round: one error, one success — loop completes normally."""
    source = FakeToolSource()
    registry = ToolRegistry()
    registry.add_source(source)
    provider = StubProvider(script=[
        _turn(calls=[
            _call("no_such_tool", {}, idx=0),         # will error
            _call("mnesis_query", {"query": "x"}, idx=1),  # will succeed
        ]),
        _final("Got partial results."),
    ])
    profile = AgentProfile(system="sys", max_iterations=5)
    result = run(run_agent(profile, "q", TOOLS, provider, registry))

    assert result.stop_reason == "end_turn"
    tool_steps = [s for s in result.transcript if isinstance(s, ToolStep)]
    assert len(tool_steps) == 2
    assert any(s.is_error for s in tool_steps)
    assert any(not s.is_error for s in tool_steps)


# ── Audit hook ────────────────────────────────────────────────────────────────


def test_audit_hook_called_for_each_step():
    events: list[dict] = []
    result = _make([
        _turn(calls=[_call("mnesis_query", {"query": "x"})]),
        _final(),
    ], audit_hook=events.append)

    kinds = [e["kind"] for e in events]
    assert "thought" in kinds
    assert "tool" in kinds


def test_audit_hook_thought_event_shape():
    events: list[dict] = []
    _make([_final("done")], audit_hook=events.append)
    thought = next(e for e in events if e["kind"] == "thought")
    assert "turn" in thought
    assert "text_length" in thought
    assert "tool_count" in thought
    assert "stop_reason" in thought


def test_audit_hook_tool_event_shape():
    events: list[dict] = []
    _make([
        _turn(calls=[_call("mnesis_query", {"query": "redis"})]),
        _final(),
    ], audit_hook=events.append)
    tool_evt = next(e for e in events if e["kind"] == "tool")
    assert tool_evt["tool"] == "mnesis_query"
    assert "call_id" in tool_evt
    assert "args_keys" in tool_evt       # keys only — values are redacted
    assert "query" in tool_evt["args_keys"]
    assert "status" in tool_evt          # "ok" or "error"


def test_audit_hook_args_values_not_exposed():
    """The audit hook must not emit arg values (only keys) — privacy guardrail."""
    events: list[dict] = []
    _make([
        _turn(calls=[_call("mnesis_query", {"query": "secret-term"})]),
        _final(),
    ], audit_hook=events.append)
    tool_evt = next(e for e in events if e["kind"] == "tool")
    assert "secret-term" not in str(tool_evt)


# ── Citation extractor unit tests ─────────────────────────────────────────────


def test_default_extractor_query_hits():
    result = _extract_citations("mnesis_query", '{"hits": [{"id": "p1"}, {"id": "p2"}]}')
    assert result == ["p1", "p2"]


def test_default_extractor_direct_id():
    assert _extract_citations("mnesis_get", '{"id": "page-abc"}') == ["page-abc"]


def test_default_extractor_page_id_field():
    assert _extract_citations("mnesis_ingest", '{"action_taken": "new", "page_id": "p99"}') == ["p99"]


def test_default_extractor_digest_id_field():
    assert _extract_citations("mnesis_file_back", '{"filed": true, "digest_id": "d7"}') == ["d7"]


def test_default_extractor_invalid_json_returns_empty():
    assert _extract_citations("any", "not json") == []
    assert _extract_citations("any", "") == []


def test_default_extractor_no_id_fields_returns_empty():
    assert _extract_citations("any", '{"foo": "bar"}') == []


# ── AgentResult shape ─────────────────────────────────────────────────────────


def test_agent_result_has_all_fields():
    result = _make([_final("done")])
    assert isinstance(result.final_text, str)
    assert isinstance(result.transcript, list)
    assert isinstance(result.tools_used, list)
    assert isinstance(result.citations, list)
    assert isinstance(result.writes, list)
    assert isinstance(result.stop_reason, str)
    assert isinstance(result.usage, dict)
    assert isinstance(result.iterations, int)
    assert "input_tokens" in result.usage
    assert "output_tokens" in result.usage
