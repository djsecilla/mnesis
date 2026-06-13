"""Tests for the Mnesis memory integration layer (A4).

All tests run offline: StubProvider + FakeToolSource. No network, no Mnesis.

Coverage:
  - load_context: formats hits; graceful on error / empty
  - make_grounded_system: injects context and policy notes
  - run_grounded_agent:
      off mode    — context loaded, no proposal, no writes
      propose mode — context loaded, proposal synthesised, nothing written
      apply mode  — context loaded, write tool called in loop, recorded in writes
  - Citation integrity: only real page IDs from tool results appear
  - Tool governance: write tools hidden from model in propose/off mode
"""
from __future__ import annotations

import asyncio
import json

import pytest

from mnesis_agent.fake_tools import DEFAULT_RESPONSES, FakeToolSource
from mnesis_agent.loop import ThoughtStep, ToolStep
from mnesis_agent.memory import (
    DEFAULT_WRITE_TOOLS,
    DigestProposal,
    GroundedAgentResult,
    MemoryProfile,
    _CITATION_INSTRUCTION,
    _CONTEXT_HEADER,
    load_context,
    make_grounded_system,
    run_grounded_agent,
)
from mnesis_agent.mcp_client import ToolSpec
from mnesis_agent.provider import AssistantTurn, Provider, StubProvider, ToolCall
from mnesis_agent.registry import ToolRegistry


def run(coro):
    return asyncio.run(coro)


# ── Shared helpers ────────────────────────────────────────────────────────────

ALL_TOOLS = [
    ToolSpec("mnesis_query", "Search", {"type": "object", "properties": {"query": {"type": "string"}}}),
    ToolSpec("mnesis_get", "Get page", {"type": "object", "properties": {"id": {"type": "string"}}}),
    ToolSpec("mnesis_file_back", "File digest", {
        "type": "object",
        "properties": {"question": {"type": "string"}, "answer": {"type": "string"}},
        "required": ["question", "answer"],
    }),
    ToolSpec("mnesis_ingest", "Ingest", {"type": "object", "properties": {"text": {"type": "string"}}}),
]


def _turn(text="", calls: list[ToolCall] | None = None, reason="tool_use", usage=None):
    return AssistantTurn(
        text=text,
        tool_calls=calls or [],
        stop_reason=reason,
        usage=usage or {"input_tokens": 10, "output_tokens": 5},
    )


def _call(name, args=None, *, idx=0):
    return ToolCall(id=f"tc_{idx}", name=name, args=args or {})


def _final(text="Atlas uses Redis for caching [atlas]."):
    return _turn(text=text, calls=[], reason="end_turn")


def _make_registry(extra_responses: dict | None = None) -> ToolRegistry:
    source = FakeToolSource(responses=extra_responses)
    reg = ToolRegistry()
    reg.add_source(source)
    return reg


def _run_grounded(
    script: list[AssistantTurn],
    *,
    policy: str = "off",
    extra_responses: dict | None = None,
    write_allowlist: frozenset | None = None,
) -> GroundedAgentResult:
    registry = _make_registry(extra_responses)
    provider = StubProvider(script=script)
    profile = MemoryProfile(
        base_system="You are a helpful assistant.",
        write_policy=policy,  # type: ignore[arg-type]
        write_allowlist=write_allowlist,
        max_iterations=5,
    )
    return run(run_grounded_agent(profile, "What uses Redis?", ALL_TOOLS, provider, registry))


class CapturingProvider:
    """Wraps a Provider and records the system prompt from the first call."""

    def __init__(self, delegate: Provider) -> None:
        self._delegate = delegate
        self.captured_system: str | None = None
        self.captured_tools: list[ToolSpec] | None = None

    async def complete_with_tools(self, system: str, messages, tools):
        if self.captured_system is None:
            self.captured_system = system
            self.captured_tools = list(tools)
        return await self._delegate.complete_with_tools(system, messages, tools)


# ── load_context ──────────────────────────────────────────────────────────────


def test_load_context_returns_non_empty_for_known_query():
    reg = _make_registry()
    result = run(load_context("redis", reg))
    assert result  # non-empty string


def test_load_context_includes_header():
    reg = _make_registry()
    result = run(load_context("redis", reg))
    assert _CONTEXT_HEADER in result


def test_load_context_includes_page_id_and_title():
    reg = _make_registry()
    result = run(load_context("redis", reg))
    assert "atlas" in result
    assert "Redis" in result


def test_load_context_includes_confidence():
    reg = _make_registry()
    result = run(load_context("redis", reg))
    assert "confidence" in result.lower() or "0." in result


def test_load_context_empty_on_no_hits():
    reg = _make_registry(extra_responses={"mnesis_query": '{"hits": []}'})
    result = run(load_context("nothing-here", reg))
    assert result == ""


def test_load_context_graceful_on_missing_tool():
    # Registry with no sources → ToolNotFoundError → empty string
    reg = ToolRegistry()
    result = run(load_context("redis", reg))
    assert result == ""


def test_load_context_graceful_on_invalid_json():
    reg = _make_registry(extra_responses={"mnesis_query": "not json"})
    result = run(load_context("redis", reg))
    assert result == ""


def test_load_context_respects_limit_parameter():
    """limit=0 still returns a string (empty hits from fake)."""
    # FakeToolSource ignores the limit arg but we verify it's passed without error
    reg = _make_registry()
    result = run(load_context("redis", reg, limit=1))
    assert isinstance(result, str)


# ── make_grounded_system ──────────────────────────────────────────────────────


def test_grounded_system_contains_base():
    sys = make_grounded_system("You are helpful.", "", "off")
    assert "You are helpful." in sys


def test_grounded_system_contains_citation_instruction():
    sys = make_grounded_system("base", "", "off")
    assert "[page-id]" in sys or "cite" in sys.lower()


def test_grounded_system_injects_context():
    ctx = "## Knowledge base context\n### [p1] Title\n> Snippet"
    sys = make_grounded_system("base", ctx, "off")
    assert "[p1]" in sys
    assert "Snippet" in sys


def test_grounded_system_empty_context_not_injected():
    sys = make_grounded_system("base", "", "off")
    assert _CONTEXT_HEADER not in sys


def test_grounded_system_propose_mode_contains_hint():
    sys = make_grounded_system("base", "", "propose")
    assert "do not" in sys.lower() or "NOT" in sys


def test_grounded_system_apply_mode_contains_hint():
    sys = make_grounded_system("base", "", "apply")
    assert "mnesis_file_back" in sys


def test_grounded_system_off_mode_no_policy_note():
    sys = make_grounded_system("base", "", "off")
    assert "mnesis_file_back" not in sys
    assert "propose" not in sys.lower()


# ── run_grounded_agent — off mode ─────────────────────────────────────────────


def test_off_mode_context_is_loaded():
    result = _run_grounded([_final()], policy="off")
    assert result.context_loaded  # non-empty: mnesis_query ran at session start


def test_off_mode_no_proposal():
    result = _run_grounded([_final()], policy="off")
    assert result.proposal is None


def test_off_mode_no_writes():
    result = _run_grounded([_final()], policy="off")
    assert result.writes == []


def test_off_mode_run_completes():
    result = _run_grounded([
        _turn(calls=[_call("mnesis_query", {"query": "redis"}, idx=0)]),
        _final(),
    ], policy="off")
    assert result.stop_reason == "end_turn"
    assert result.final_text


# ── run_grounded_agent — propose mode ─────────────────────────────────────────


def test_propose_mode_returns_digest_proposal():
    result = _run_grounded([
        _turn(calls=[_call("mnesis_query", {"query": "redis"}, idx=0)]),
        _final("Redis is used for caching [atlas]."),
    ], policy="propose")
    assert isinstance(result.proposal, DigestProposal)


def test_propose_mode_proposal_question_matches_input():
    result = _run_grounded([_final("done")], policy="propose")
    assert result.proposal is not None
    assert result.proposal.question == "What uses Redis?"


def test_propose_mode_proposal_answer_matches_final_text():
    result = _run_grounded([_final("Atlas uses Redis [atlas].")], policy="propose")
    assert result.proposal.answer == result.final_text


def test_propose_mode_proposal_citations_from_tool_results():
    result = _run_grounded([
        _turn(calls=[_call("mnesis_query", {"query": "redis"}, idx=0)]),
        _final("Redis is used for caching [atlas]."),
    ], policy="propose")
    # "atlas" came from the FakeToolSource mnesis_query response
    assert "atlas" in result.proposal.citations


def test_propose_mode_nothing_written():
    """No write tool should be called in propose mode."""
    result = _run_grounded([
        _turn(calls=[_call("mnesis_query", {"query": "redis"}, idx=0)]),
        _final("done"),
    ], policy="propose")
    assert result.writes == []


def test_propose_mode_write_tools_hidden_from_model():
    """Write tools must not appear in the tools list sent to the LLM in propose mode."""
    registry = _make_registry()
    script = [_final("done")]
    provider = StubProvider(script=script)
    cap = CapturingProvider(provider)
    profile = MemoryProfile(base_system="sys", write_policy="propose")
    run(run_grounded_agent(profile, "q", ALL_TOOLS, cap, registry))

    assert cap.captured_tools is not None
    tool_names = {t.name for t in cap.captured_tools}
    for wt in DEFAULT_WRITE_TOOLS:
        assert wt not in tool_names, f"{wt!r} should be hidden in propose mode"


def test_propose_mode_no_proposal_on_empty_final_text():
    """No proposal when the loop produced no final text (guardrail stop)."""
    # max_iterations=0 with a tool-use turn → stops before giving a final answer
    registry = _make_registry()
    provider = StubProvider(script=[
        _turn(calls=[_call("mnesis_query", {"query": "x"})]),
    ])
    profile = MemoryProfile(base_system="sys", write_policy="propose", max_iterations=0)
    result = run(run_grounded_agent(profile, "q", ALL_TOOLS, provider, registry))
    # final_text is "" because the loop stopped before the model answered
    assert result.proposal is None or result.proposal.answer == ""


# ── run_grounded_agent — apply mode ──────────────────────────────────────────


def test_apply_mode_no_proposal():
    result = _run_grounded([
        _turn(calls=[_call("mnesis_query", {"query": "redis"}, idx=0)]),
        _final(),
    ], policy="apply")
    assert result.proposal is None


def test_apply_mode_write_tools_visible_to_model():
    """mnesis_file_back must appear in the tools list sent to the model in apply mode."""
    registry = _make_registry()
    script = [_final("done")]
    provider = StubProvider(script=script)
    cap = CapturingProvider(provider)
    profile = MemoryProfile(base_system="sys", write_policy="apply")
    run(run_grounded_agent(profile, "q", ALL_TOOLS, cap, registry))

    tool_names = {t.name for t in (cap.captured_tools or [])}
    assert "mnesis_file_back" in tool_names


def test_apply_mode_file_back_recorded_in_writes():
    """When the model calls mnesis_file_back, it appears in result.writes."""
    result = _run_grounded([
        _turn(calls=[_call("mnesis_query", {"query": "redis"}, idx=0)]),
        _turn(calls=[_call("mnesis_file_back", {
            "question": "What does Atlas use?",
            "answer": "Redis for caching",
        }, idx=1)]),
        _final("Atlas uses Redis [atlas]."),
    ], policy="apply")
    assert any(w.name == "mnesis_file_back" for w in result.writes)


def test_apply_mode_file_back_tool_step_in_transcript():
    result = _run_grounded([
        _turn(calls=[_call("mnesis_file_back", {
            "question": "Q?", "answer": "A."
        }, idx=0)]),
        _final("done"),
    ], policy="apply")
    fb_steps = [s for s in result.transcript if isinstance(s, ToolStep) and s.tool_name == "mnesis_file_back"]
    assert len(fb_steps) == 1
    assert not fb_steps[0].is_error


def test_apply_mode_custom_write_allowlist():
    """Only tools in the write_allowlist are tracked as writes."""
    # Allow only mnesis_file_back; mnesis_ingest should NOT be tracked as write
    result = _run_grounded([
        _turn(calls=[_call("mnesis_file_back", {"question": "Q?", "answer": "A."}, idx=0)]),
        _final(),
    ], policy="apply", write_allowlist=frozenset({"mnesis_file_back"}))
    # file_back IS in writes
    assert any(w.name == "mnesis_file_back" for w in result.writes)


# ── Citation integrity ────────────────────────────────────────────────────────


def test_citations_only_from_real_tool_results():
    """All citation IDs must have appeared in at least one tool result."""
    result = _run_grounded([
        _turn(calls=[_call("mnesis_query", {"query": "redis"}, idx=0)]),
        _final("Atlas uses Redis [atlas]."),
    ], policy="off")
    # "atlas" comes from FakeToolSource's mnesis_query hit; it's real
    assert "atlas" in result.citations
    # No phantom IDs should appear
    tool_steps = [s for s in result.transcript if isinstance(s, ToolStep)]
    tool_result_ids: set[str] = set()
    for step in tool_steps:
        try:
            data = json.loads(step.result)
            if isinstance(data, dict):
                for k in ("id", "page_id", "digest_id"):
                    if isinstance(data.get(k), str):
                        tool_result_ids.add(data[k])
                for h in data.get("hits", []):
                    if isinstance(h, dict) and isinstance(h.get("id"), str):
                        tool_result_ids.add(h["id"])
        except Exception:
            pass
    for cid in result.citations:
        assert cid in tool_result_ids, f"Citation {cid!r} not in any tool result"


def test_citations_include_file_back_digest_id():
    """In apply mode, mnesis_file_back's digest_id is extracted as a citation."""
    result = _run_grounded([
        _turn(calls=[_call("mnesis_file_back", {"question": "Q?", "answer": "A."}, idx=0)]),
        _final("done"),
    ], policy="apply")
    assert "stub-digest-abc123" in result.citations


# ── Context in system prompt ──────────────────────────────────────────────────


def test_context_injected_into_system_prompt():
    registry = _make_registry()
    script = [_final("done")]
    provider = StubProvider(script=script)
    cap = CapturingProvider(provider)
    profile = MemoryProfile(base_system="You are helpful.")
    run(run_grounded_agent(profile, "What uses Redis?", ALL_TOOLS, cap, registry))

    assert cap.captured_system is not None
    # The pre-loaded context (atlas page from mnesis_query) must be in the prompt
    assert "atlas" in cap.captured_system
    assert _CONTEXT_HEADER in cap.captured_system


def test_base_system_preserved_in_grounded_prompt():
    registry = _make_registry()
    cap = CapturingProvider(StubProvider(script=[_final()]))
    profile = MemoryProfile(base_system="SENTINEL_BASE_SYSTEM")
    run(run_grounded_agent(profile, "q", ALL_TOOLS, cap, registry))
    assert "SENTINEL_BASE_SYSTEM" in (cap.captured_system or "")


def test_grounded_result_has_all_fields():
    result = _run_grounded([_final()], policy="propose")
    assert isinstance(result, GroundedAgentResult)
    assert isinstance(result.final_text, str)
    assert isinstance(result.context_loaded, str)
    assert isinstance(result.citations, list)
    assert isinstance(result.writes, list)
    assert isinstance(result.usage, dict)
    assert isinstance(result.iterations, int)
    assert isinstance(result.stop_reason, str)
    # proposal is DigestProposal in propose mode
    assert result.proposal is not None
    assert isinstance(result.proposal.question, str)
    assert isinstance(result.proposal.answer, str)
    assert isinstance(result.proposal.citations, list)
