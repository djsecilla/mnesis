"""Tests for policy enforcement, budgets, the run audit, and local tools (A6).

All offline: StubProvider + a recording FakeToolSource. No network, no Mnesis.
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
from pathlib import Path

import pytest

from mnesis_agent.audit import AuditLog, new_run_id, read_run_records
from mnesis_agent.fake_tools import FakeToolSource
from mnesis_agent.local_tools import (
    LocalToolSource,
    build_local_tool_source,
    make_example_local_source,
)
from mnesis_agent.loop import ThoughtStep, ToolStep
from mnesis_agent.mcp_client import MCPToolError, ToolSource, ToolSpec
from mnesis_agent.policy import (
    PolicyEnforcingRegistry,
    PolicyViolation,
    ToolPolicy,
)
from mnesis_agent.profiles import ASSISTANT, INGEST_DAEMON, RESEARCH, Archetype
from mnesis_agent.provider import AssistantTurn, StubProvider, ToolCall
from mnesis_agent.registry import ToolRegistry
from mnesis_agent.runner import build_registry, run_archetype


def run(coro):
    return asyncio.run(coro)


# ── Recording source ──────────────────────────────────────────────────────────


class RecordingSource(ToolSource):
    def __init__(self, responses=None, tools=None):
        self._inner = FakeToolSource(responses=responses, tools=tools)
        self.calls: list[tuple[str, dict]] = []

    async def list_tools(self):
        return await self._inner.list_tools()

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        return await self._inner.call_tool(name, args)

    def count(self, name):
        return sum(1 for n, _ in self.calls if n == name)


FULL_TOOLS = [
    ToolSpec("mnesis_query", "Search", {"type": "object", "properties": {"query": {"type": "string"}}}),
    ToolSpec("mnesis_get", "Get", {"type": "object", "properties": {"id": {"type": "string"}}}),
    ToolSpec("mnesis_entity", "Entity", {"type": "object", "properties": {"ref": {"type": "string"}}}),
    ToolSpec("mnesis_traverse", "Traverse", {"type": "object", "properties": {"ref": {"type": "string"}}}),
    ToolSpec("mnesis_impact", "Impact", {"type": "object", "properties": {"entity": {"type": "string"}}}),
    ToolSpec("mnesis_file_back", "File", {"type": "object", "properties": {"question": {"type": "string"}, "answer": {"type": "string"}}}),
    ToolSpec("mnesis_ingest", "Ingest", {"type": "object", "properties": {"text": {"type": "string"}}}),
    ToolSpec("mnesis_graph_stats", "Stats", {"type": "object", "properties": {}}),
]


def _turn(text="", calls=None, reason="tool_use", usage=None):
    return AssistantTurn(text=text, tool_calls=calls or [], stop_reason=reason,
                         usage=usage or {"input_tokens": 10, "output_tokens": 5})


def _call(name, args=None, *, idx=0):
    return ToolCall(id=f"tc_{idx}", name=name, args=args or {})


def _final(text="done"):
    return _turn(text=text, calls=[], reason="end_turn")


def _registry(source):
    reg = ToolRegistry()
    reg.add_source(source)
    return reg


# ══ ToolPolicy unit tests ═════════════════════════════════════════════════════


def test_policy_allows_allowlisted_read_tool():
    p = ToolPolicy.from_archetype(ASSISTANT)
    p.check("mnesis_query")  # no raise


def test_policy_refuses_out_of_allowlist():
    p = ToolPolicy.from_archetype(ASSISTANT)
    with pytest.raises(PolicyViolation, match="not in this profile's allowlist"):
        p.check("mnesis_graph_stats")


def test_policy_refuses_write_under_propose():
    p = ToolPolicy.from_archetype(ASSISTANT)  # propose
    # file_back isn't even in the assistant allowlist → allowlist refusal first
    with pytest.raises(PolicyViolation):
        p.check("mnesis_file_back")


def test_policy_research_allows_file_back():
    p = ToolPolicy.from_archetype(RESEARCH)  # apply, write_allowlist={file_back}
    p.check("mnesis_file_back")  # no raise


def test_policy_research_refuses_ingest_not_in_allowlist():
    p = ToolPolicy.from_archetype(RESEARCH)
    with pytest.raises(PolicyViolation):
        p.check("mnesis_ingest")  # not in research allowlist


def test_policy_apply_but_write_not_in_write_allowlist():
    # An archetype whose allowlist includes ingest, but write_allowlist excludes it.
    arch = Archetype(
        name="x", system_prompt="s",
        tool_allowlist=frozenset({"mnesis_query", "mnesis_ingest"}),
        write_policy="apply",
        write_allowlist=frozenset({"mnesis_file_back"}),  # ingest NOT permitted
    )
    p = ToolPolicy.from_archetype(arch)
    with pytest.raises(PolicyViolation, match="write allowlist"):
        p.check("mnesis_ingest")


def test_policy_extra_allowed_extends_allowlist():
    p = ToolPolicy.from_archetype(RESEARCH, extra_allowed=frozenset({"web_search"}))
    p.check("web_search")  # no raise — extra tool permitted


def test_policy_daemon_allows_ingest():
    p = ToolPolicy.from_archetype(INGEST_DAEMON)
    p.check("mnesis_ingest")  # apply + ingest in write_allowlist


# ══ PolicyEnforcingRegistry ═══════════════════════════════════════════════════


def test_enforcing_registry_passes_allowed_call():
    src = RecordingSource()
    reg = _registry(src)
    enforcing = PolicyEnforcingRegistry(reg, ToolPolicy.from_archetype(ASSISTANT))
    raw = run(enforcing.dispatch("mnesis_query", {"query": "redis"}))
    assert json.loads(raw)["hits"]
    assert src.count("mnesis_query") == 1


def test_enforcing_registry_refuses_before_dispatch():
    """A refused call must never reach the underlying source (no side effect)."""
    src = RecordingSource()
    reg = _registry(src)
    enforcing = PolicyEnforcingRegistry(reg, ToolPolicy.from_archetype(ASSISTANT))
    with pytest.raises(PolicyViolation):
        run(enforcing.dispatch("mnesis_ingest", {"text": "x"}))
    assert src.count("mnesis_ingest") == 0  # never dispatched


def test_enforcing_registry_on_refusal_callback():
    refused: list[str] = []
    src = RecordingSource()
    enforcing = PolicyEnforcingRegistry(
        _registry(src), ToolPolicy.from_archetype(ASSISTANT),
        on_refusal=refused.append,
    )
    with pytest.raises(PolicyViolation):
        run(enforcing.dispatch("mnesis_ingest", {"text": "x"}))
    assert refused == ["mnesis_ingest"]


# ══ ACCEPTANCE: out-of-allowlist refused and surfaced to the model ════════════


def test_out_of_allowlist_call_refused_and_surfaced():
    src = RecordingSource()
    reg = _registry(src)
    # The (misbehaving) model tries a tool outside the assistant's allowlist.
    provider = StubProvider(script=[
        _turn(calls=[_call("mnesis_graph_stats", {}, idx=0)]),
        _final("I could not use that tool, but here's my answer."),
    ])
    result = run(run_archetype(ASSISTANT, "stats?", reg, provider))

    # Run still completes — the refusal was fed back and the model recovered.
    assert result.stop_reason == "end_turn"

    # The refused tool never executed (no side effect).
    assert src.count("mnesis_graph_stats") == 0

    # The refusal was surfaced to the model as an error tool-result.
    tool_steps = [s for s in result.transcript if isinstance(s, ToolStep)]
    assert len(tool_steps) == 1
    assert tool_steps[0].is_error is True
    assert "allowlist" in tool_steps[0].result.lower() or "refused" in tool_steps[0].result.lower()


def test_out_of_allowlist_write_refused_for_assistant():
    src = RecordingSource()
    reg = _registry(src)
    provider = StubProvider(script=[
        _turn(calls=[_call("mnesis_ingest", {"text": "x"}, idx=0)]),
        _final("Recovered."),
    ])
    result = run(run_archetype(ASSISTANT, "ingest please", reg, provider))
    assert src.count("mnesis_ingest") == 0  # write refused, never executed
    assert result.writes == []


# ══ ACCEPTANCE: exceeding a budget stops the run with a flag ══════════════════


def test_budget_max_tool_calls_stops_with_flag():
    # A custom archetype with a tight tool-call budget.
    arch = Archetype(
        name="tight", system_prompt="s",
        tool_allowlist=frozenset({"mnesis_query"}),
        write_policy="off",
        max_tool_calls=1, max_iterations=5,
    )
    src = RecordingSource()
    reg = _registry(src)
    provider = StubProvider(script=[
        _turn(calls=[_call("mnesis_query", {"query": "a"}, idx=0)]),
        _turn(calls=[_call("mnesis_query", {"query": "b"}, idx=1)]),
        _final(),
    ])
    result = run(run_archetype(arch, "q", reg, provider))
    assert result.stop_reason == "max_tool_calls"   # flagged, deterministic stop


def test_budget_max_iterations_stops_with_flag():
    arch = Archetype(
        name="tight2", system_prompt="s",
        tool_allowlist=frozenset({"mnesis_query"}),
        write_policy="off",
        max_iterations=1,
    )
    src = RecordingSource()
    reg = _registry(src)
    provider = StubProvider(script=[
        _turn(calls=[_call("mnesis_query", {"query": "a"}, idx=0)]),
        _turn(calls=[_call("mnesis_query", {"query": "b"}, idx=1)]),
        _final(),
    ])
    result = run(run_archetype(arch, "q", reg, provider))
    assert result.stop_reason == "max_iterations"


# ══ ACCEPTANCE: audit — one record per step, no leaked values ═════════════════


def _read_all_records(directory: Path) -> list[dict]:
    out: list[dict] = []
    for fp in sorted(glob.glob(str(Path(directory) / "*.jsonl"))):
        with open(fp, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
    return out


def test_audit_one_record_per_step_no_leaks(tmp_path):
    secret = "hunter2-SUPER-SECRET-token"
    src = RecordingSource()
    reg = _registry(src)
    audit = AuditLog(tmp_path)
    provider = StubProvider(script=[
        _turn(calls=[_call("mnesis_query", {"query": secret}, idx=0)]),       # secret in args
        _turn(calls=[_call("mnesis_file_back", {"question": "q", "answer": secret}, idx=1)]),
        _final("Final answer."),
    ])
    result = run(run_archetype(RESEARCH, secret, reg, provider, audit=audit))

    records = _read_all_records(tmp_path)
    blob = json.dumps(records)

    # exactly one run_start and one run_end
    assert sum(1 for r in records if r["type"] == "run_start") == 1
    assert sum(1 for r in records if r["type"] == "run_end") == 1

    # one step record per transcript entry (thought + tool steps)
    step_records = [r for r in records if r["type"] == "step"]
    assert len(step_records) == len(result.transcript)

    # NO leaked argument value anywhere in the audit (only keys are logged).
    # The user input is recorded, so check the secret doesn't appear in step/
    # run_end records, and that step tool records carry args_keys not values.
    for r in step_records:
        assert secret not in json.dumps(r)
        if r.get("kind") == "tool":
            assert "args_keys" in r
            assert "args" not in r  # never the values
            assert "result" not in r  # never the result body

    end = next(r for r in records if r["type"] == "run_end")
    assert secret not in json.dumps(end)
    # writes recorded as tool + call_id only
    for w in end["writes"]:
        assert set(w) == {"tool", "call_id"}


def test_audit_run_start_has_profile_and_input(tmp_path):
    src = RecordingSource()
    audit = AuditLog(tmp_path)
    provider = StubProvider(script=[_final("answer")])
    run(run_archetype(ASSISTANT, "what is redis", _registry(src), provider, audit=audit))

    records = _read_all_records(tmp_path)
    start = next(r for r in records if r["type"] == "run_start")
    assert start["profile"] == "assistant"
    assert start["input"] == "what is redis"
    assert "run_id" in start and "ts" in start


def test_audit_run_end_has_usage_and_stop_reason(tmp_path):
    src = RecordingSource()
    audit = AuditLog(tmp_path)
    provider = StubProvider(script=[_final("answer")])
    run(run_archetype(ASSISTANT, "q", _registry(src), provider, audit=audit))
    records = _read_all_records(tmp_path)
    end = next(r for r in records if r["type"] == "run_end")
    assert end["stop_reason"] == "end_turn"
    assert "input_tokens" in end["usage"]
    assert "tools_used" in end


def test_audit_records_share_one_run_id(tmp_path):
    src = RecordingSource()
    audit = AuditLog(tmp_path)
    provider = StubProvider(script=[
        _turn(calls=[_call("mnesis_query", {"query": "x"})]),
        _final(),
    ])
    run(run_archetype(ASSISTANT, "q", _registry(src), provider, audit=audit))
    records = _read_all_records(tmp_path)
    run_ids = {r["run_id"] for r in records}
    assert len(run_ids) == 1
    rid = run_ids.pop()
    assert read_run_records(tmp_path, rid) == records


def test_audit_disabled_writes_nothing(tmp_path):
    src = RecordingSource()
    provider = StubProvider(script=[_final("answer")])
    # No audit passed → no files written.
    run(run_archetype(ASSISTANT, "q", _registry(src), provider))
    assert _read_all_records(tmp_path) == []


def test_audit_appends_across_runs(tmp_path):
    audit = AuditLog(tmp_path)
    for q in ("first", "second"):
        run(run_archetype(ASSISTANT, q, _registry(RecordingSource()),
                          StubProvider(script=[_final("a")]), audit=audit))
    records = _read_all_records(tmp_path)
    starts = [r for r in records if r["type"] == "run_start"]
    assert len(starts) == 2
    assert {s["input"] for s in starts} == {"first", "second"}


# ══ ACCEPTANCE: local tool callable only when flag set + research only ════════


def test_local_tool_source_disabled_by_default(monkeypatch):
    import mnesis_agent.config as ac
    monkeypatch.setattr(ac, "MNESIS_AGENT_ENABLE_LOCAL_TOOLS", False)
    import mnesis_agent.local_tools as lt
    monkeypatch.setattr(lt.config, "MNESIS_AGENT_ENABLE_LOCAL_TOOLS", False)
    assert build_local_tool_source() is None


def test_local_tool_source_enabled_with_flag(monkeypatch):
    import mnesis_agent.local_tools as lt
    monkeypatch.setattr(lt.config, "MNESIS_AGENT_ENABLE_LOCAL_TOOLS", True)
    src = build_local_tool_source()
    assert src is not None
    assert "web_search" in src.tool_names()


def test_example_local_tool_is_callable():
    src = make_example_local_source()
    raw = run(src.call_tool("web_search", {"query": "redis"}))
    data = json.loads(raw)
    assert data["query"] == "redis"
    assert "results" in data


def test_local_tool_callable_for_research_when_registered():
    """Research + registered local tool → the web_search call is permitted."""
    local = make_example_local_source()
    mnesis = RecordingSource()
    reg = build_registry([mnesis, local])
    local_names = local.tool_names()

    provider = StubProvider(script=[
        _turn(calls=[_call("web_search", {"query": "redis"}, idx=0)]),
        _final("Synthesized with web context [atlas]."),
    ])
    result = run(run_archetype(
        RESEARCH, "research redis", reg, provider, local_tool_names=local_names
    ))
    # web_search executed (no policy refusal) and recorded in the transcript
    tool_steps = [s for s in result.transcript if isinstance(s, ToolStep)]
    ws = [s for s in tool_steps if s.tool_name == "web_search"]
    assert len(ws) == 1
    assert ws[0].is_error is False
    assert "web_search" in result.tools_used


def test_local_tool_refused_for_assistant_even_when_registered():
    """Assistant must NOT be able to call a local tool, even if it's registered."""
    local = make_example_local_source()
    mnesis = RecordingSource()
    reg = build_registry([mnesis, local])
    local_names = local.tool_names()

    provider = StubProvider(script=[
        _turn(calls=[_call("web_search", {"query": "redis"}, idx=0)]),
        _final("Could not use web search."),
    ])
    # Even if we pass local_tool_names, the assistant does not allow local tools,
    # so the policy layer ignores them for this profile.
    result = run(run_archetype(
        ASSISTANT, "search redis", reg, provider, local_tool_names=local_names
    ))
    tool_steps = [s for s in result.transcript if isinstance(s, ToolStep)]
    ws = [s for s in tool_steps if s.tool_name == "web_search"]
    assert len(ws) == 1
    assert ws[0].is_error is True  # refused → surfaced as error
    assert "allowlist" in ws[0].result.lower() or "refused" in ws[0].result.lower()


def test_local_tool_not_callable_when_not_passed_to_research():
    """Even research can't call a local tool unless its names are passed in."""
    local = make_example_local_source()
    reg = build_registry([RecordingSource(), local])
    provider = StubProvider(script=[
        _turn(calls=[_call("web_search", {"query": "x"}, idx=0)]),
        _final("recovered"),
    ])
    # local_tool_names omitted → web_search not in the effective allowlist
    result = run(run_archetype(RESEARCH, "q", reg, provider))
    ws = [s for s in result.transcript if isinstance(s, ToolStep) and s.tool_name == "web_search"]
    assert len(ws) == 1 and ws[0].is_error is True


def test_plain_run_starts_with_only_mnesis_tools():
    """Without local tools, the registry exposes only Mnesis tools."""
    reg = build_registry([FakeToolSource()])  # no local_tools arg
    names = {t.name for t in run(reg.list_tools())}
    assert "web_search" not in names
    assert "mnesis_query" in names


# ══ LocalToolSource unit ══════════════════════════════════════════════════════


def test_local_tool_source_unknown_tool_raises():
    src = LocalToolSource()
    with pytest.raises(MCPToolError):
        run(src.call_tool("nope", {}))


def test_new_run_id_unique():
    assert new_run_id() != new_run_id()
