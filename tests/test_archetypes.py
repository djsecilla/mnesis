"""Tests for the three archetypes, their plumbing, and the ingest daemon (A5).

All offline: StubProvider scripts the LLM turns; FakeToolSource (optionally
recording) stands in for Mnesis. No network, no Mnesis process.
"""
from __future__ import annotations

import asyncio
import json
import logging

import pytest

from mnesis_agent.daemon import IngestDaemon, source_ref_for, _parse_ingest_result
from mnesis_agent.fake_tools import FakeToolSource
from mnesis_agent.loop import ToolStep
from mnesis_agent.mcp_client import ToolSource, ToolSpec
from mnesis_agent.profiles import (
    ARCHETYPES,
    ASSISTANT,
    INGEST_DAEMON,
    RESEARCH,
    get_archetype,
    to_memory_profile,
)
from mnesis_agent.profiles.base import filter_to_allowlist
from mnesis_agent.provider import AssistantTurn, StubProvider, ToolCall
from mnesis_agent.registry import ToolRegistry
from mnesis_agent.runner import (
    build_registry,
    confirm_and_file,
    extract_digest_id,
    run_archetype,
)


def run(coro):
    return asyncio.run(coro)


# ── Recording source: counts every tool dispatch, delegates to a fake ─────────


class RecordingSource(ToolSource):
    """Wraps a FakeToolSource and records (name, args) of every call."""

    def __init__(self, responses: dict | None = None, tools: list[ToolSpec] | None = None):
        self._inner = FakeToolSource(responses=responses, tools=tools)
        self.calls: list[tuple[str, dict]] = []

    async def list_tools(self) -> list[ToolSpec]:
        return await self._inner.list_tools()

    async def call_tool(self, name: str, args: dict) -> str:
        self.calls.append((name, args))
        return await self._inner.call_tool(name, args)

    def count(self, name: str) -> int:
        return sum(1 for n, _ in self.calls if n == name)


# Full tool catalogue so allowlist filtering has graph tools to find.
FULL_TOOLS = [
    ToolSpec("mnesis_query", "Search", {"type": "object", "properties": {"query": {"type": "string"}}}),
    ToolSpec("mnesis_get", "Get", {"type": "object", "properties": {"id": {"type": "string"}}}),
    ToolSpec("mnesis_entity", "Entity", {"type": "object", "properties": {"ref": {"type": "string"}}}),
    ToolSpec("mnesis_impact", "Impact", {"type": "object", "properties": {"entity": {"type": "string"}}}),
    ToolSpec("mnesis_neighbors", "Neighbors", {"type": "object", "properties": {"ref": {"type": "string"}}}),
    ToolSpec("mnesis_traverse", "Traverse", {"type": "object", "properties": {"ref": {"type": "string"}}}),
    ToolSpec("mnesis_file_back", "File", {"type": "object", "properties": {"question": {"type": "string"}, "answer": {"type": "string"}}}),
    ToolSpec("mnesis_ingest", "Ingest", {"type": "object", "properties": {"text": {"type": "string"}}}),
    ToolSpec("mnesis_resolve", "Resolve", {"type": "object", "properties": {"review_id": {"type": "integer"}}}),
]


def _turn(text="", calls=None, reason="tool_use", usage=None):
    return AssistantTurn(text=text, tool_calls=calls or [], stop_reason=reason,
                         usage=usage or {"input_tokens": 10, "output_tokens": 5})


def _call(name, args=None, *, idx=0):
    return ToolCall(id=f"tc_{idx}", name=name, args=args or {})


def _final(text="done"):
    return _turn(text=text, calls=[], reason="end_turn")


def _registry(source) -> ToolRegistry:
    reg = ToolRegistry()
    reg.add_source(source)
    return reg


# ── Profile sanity ─────────────────────────────────────────────────────────────


def test_archetypes_registered():
    assert set(ARCHETYPES) == {"assistant", "research", "ingest-daemon"}
    assert get_archetype("assistant") is ASSISTANT
    assert get_archetype("research") is RESEARCH
    assert get_archetype("ingest-daemon") is INGEST_DAEMON


def test_get_archetype_unknown_raises():
    with pytest.raises(KeyError):
        get_archetype("nope")


def test_assistant_profile_shape():
    assert ASSISTANT.write_policy == "propose"
    assert "mnesis_file_back" not in ASSISTANT.tool_allowlist
    assert "mnesis_ingest" not in ASSISTANT.tool_allowlist
    assert {"mnesis_query", "mnesis_get"} <= ASSISTANT.tool_allowlist
    assert ASSISTANT.entry_mode == "interactive"


def test_research_profile_shape():
    assert RESEARCH.write_policy == "apply"
    assert RESEARCH.write_allowlist == frozenset({"mnesis_file_back"})
    assert "mnesis_file_back" in RESEARCH.tool_allowlist
    # never supersedes / ingests raw sources
    assert "mnesis_ingest" not in RESEARCH.tool_allowlist
    assert "mnesis_resolve" not in RESEARCH.tool_allowlist
    assert RESEARCH.entry_mode == "batch"


def test_ingest_daemon_profile_shape():
    assert INGEST_DAEMON.write_policy == "apply"
    assert INGEST_DAEMON.write_allowlist == frozenset({"mnesis_ingest"})
    assert "mnesis_ingest" in INGEST_DAEMON.tool_allowlist
    assert {"mnesis_query", "mnesis_get"} <= INGEST_DAEMON.tool_allowlist
    # never resolves contradictions / files digests
    assert "mnesis_resolve" not in INGEST_DAEMON.tool_allowlist
    assert "mnesis_file_back" not in INGEST_DAEMON.tool_allowlist
    assert INGEST_DAEMON.entry_mode == "daemon"


def test_to_memory_profile_carries_fields():
    mp = to_memory_profile(RESEARCH)
    assert mp.write_policy == "apply"
    assert mp.write_allowlist == frozenset({"mnesis_file_back"})
    assert mp.max_iterations == RESEARCH.max_iterations
    assert mp.base_system == RESEARCH.system_prompt


def test_filter_to_allowlist():
    filtered = filter_to_allowlist(FULL_TOOLS, ASSISTANT.tool_allowlist)
    names = {t.name for t in filtered}
    assert names <= ASSISTANT.tool_allowlist
    assert "mnesis_ingest" not in names
    assert "mnesis_file_back" not in names


# ── build_registry ─────────────────────────────────────────────────────────────


def test_build_registry_with_explicit_sources():
    src = FakeToolSource()
    reg = build_registry([src])
    tools = run(reg.list_tools())
    assert {t.name for t in tools} >= {"mnesis_query", "mnesis_get"}


# ── Assistant: cited answer + non-applied proposal ────────────────────────────


def test_assistant_produces_cited_answer_and_unapplied_proposal():
    src = RecordingSource()
    reg = _registry(src)
    provider = StubProvider(script=[
        _turn(calls=[_call("mnesis_query", {"query": "redis"}, idx=0)]),
        _final("Project Atlas uses Redis for caching [atlas]."),
    ])
    result = run(run_archetype(ASSISTANT, "What uses Redis?", reg, provider))

    # cited answer grounded in a real returned page
    assert "atlas" in result.citations
    assert "[atlas]" in result.final_text

    # propose-only: a proposal is returned but NOTHING was written
    assert result.proposal is not None
    assert result.proposal.question == "What uses Redis?"
    assert result.proposal.answer == result.final_text
    assert "atlas" in result.proposal.citations
    assert result.writes == []
    assert src.count("mnesis_file_back") == 0  # never filed by the agent


def test_assistant_only_sees_allowlisted_tools():
    """The model is offered only read tools (no write tools) under propose mode."""
    captured: dict = {}

    class Cap(StubProvider):
        async def complete_with_tools(self, system, messages, tools):
            captured.setdefault("tools", [t.name for t in tools])
            return await super().complete_with_tools(system, messages, tools)

    src = FakeToolSource(tools=FULL_TOOLS)
    reg = _registry(src)
    provider = Cap(script=[_final("answer")])
    run(run_archetype(ASSISTANT, "q", reg, provider))

    offered = set(captured["tools"])
    assert "mnesis_file_back" not in offered
    assert "mnesis_ingest" not in offered
    assert offered <= ASSISTANT.tool_allowlist


def test_assistant_confirm_files_back():
    """Confirming a proposal calls mnesis_file_back exactly once (human-in-loop)."""
    src = RecordingSource()
    reg = _registry(src)
    provider = StubProvider(script=[
        _turn(calls=[_call("mnesis_query", {"query": "redis"})]),
        _final("Atlas uses Redis [atlas]."),
    ])
    result = run(run_archetype(ASSISTANT, "What uses Redis?", reg, provider))
    assert result.proposal is not None

    raw = run(confirm_and_file(result.proposal, reg))
    assert json.loads(raw)["filed"] is True
    assert src.count("mnesis_file_back") == 1


# ── Research: bounded, files exactly one digest ───────────────────────────────


def test_research_completes_within_budget_files_one_digest():
    digest_page = json.dumps({
        "id": "stub-digest-abc123",
        "title": "What depends on Redis",
        "kind": "digest",
        "body": "Synthesized answer.",
        "status": "active",
    })
    src = RecordingSource(responses={"mnesis_get": digest_page})
    reg = _registry(src)
    provider = StubProvider(script=[
        _turn(calls=[_call("mnesis_query", {"query": "redis"}, idx=0)]),
        _turn(calls=[_call("mnesis_traverse", {"ref": "library:redis"}, idx=1)]),
        _turn(calls=[_call("mnesis_file_back", {
            "question": "What depends on Redis?",
            "answer": "Atlas and the auth migration depend on Redis [atlas].",
        }, idx=2)]),
        _final("Report: Atlas and auth-migration depend on Redis [atlas]."),
    ])
    result = run(run_archetype(RESEARCH, "What depends on Redis?", reg, provider))

    # within budget — ended normally, not on a guardrail
    assert result.stop_reason == "end_turn"
    assert result.iterations <= RESEARCH.max_iterations

    # exactly one digest filed
    assert src.count("mnesis_file_back") == 1
    fb_writes = [w for w in result.writes if w.name == "mnesis_file_back"]
    assert len(fb_writes) == 1

    # the created digest id is recoverable and visible via mnesis_get
    digest_id = extract_digest_id(result)
    assert digest_id == "stub-digest-abc123"
    fetched = json.loads(run(reg.dispatch("mnesis_get", {"id": digest_id})))
    assert fetched["id"] == "stub-digest-abc123"
    assert fetched["kind"] == "digest"


def test_research_no_proposal_in_apply_mode():
    src = FakeToolSource()
    reg = _registry(src)
    provider = StubProvider(script=[_final("Report [atlas].")])
    result = run(run_archetype(RESEARCH, "goal", reg, provider))
    assert result.proposal is None


def test_research_file_back_visible_to_model():
    captured: dict = {}

    class Cap(StubProvider):
        async def complete_with_tools(self, system, messages, tools):
            captured.setdefault("tools", [t.name for t in tools])
            return await super().complete_with_tools(system, messages, tools)

    src = FakeToolSource(tools=FULL_TOOLS)
    reg = _registry(src)
    provider = Cap(script=[_final("done")])
    run(run_archetype(RESEARCH, "q", reg, provider))

    offered = set(captured["tools"])
    assert "mnesis_file_back" in offered
    assert "mnesis_ingest" not in offered  # not in research allowlist


# ── Ingest daemon: ingest once, route contradiction, skip malformed ───────────


def test_daemon_ingests_new_file_once(tmp_path):
    f = tmp_path / "note.txt"
    f.write_text("Project Atlas uses Redis for caching.", encoding="utf-8")

    src = RecordingSource(responses={
        "mnesis_ingest": json.dumps({"action_taken": "new", "page_id": "p1", "redaction_count": 0}),
    })
    daemon = IngestDaemon(_registry(src))

    outcomes = run(daemon.scan_once(tmp_path))
    assert len(outcomes) == 1
    assert outcomes[0].status == "ingested"
    assert outcomes[0].action == "new"
    assert src.count("mnesis_ingest") == 1


def test_daemon_idempotent_reseeing_file(tmp_path):
    f = tmp_path / "note.txt"
    f.write_text("Atlas uses Redis.", encoding="utf-8")
    src = RecordingSource(responses={
        "mnesis_ingest": json.dumps({"action_taken": "new", "page_id": "p1", "redaction_count": 0}),
    })
    daemon = IngestDaemon(_registry(src))

    run(daemon.scan_once(tmp_path))           # first sight → ingest
    out2 = run(daemon.scan_once(tmp_path))     # re-sight → nothing new processed
    assert out2 == []                          # already seen, filtered out
    assert src.count("mnesis_ingest") == 1     # not duplicated

    # processing the exact same file directly is also a no-op
    out3 = run(daemon.process_file(f))
    assert out3.status == "skipped_duplicate"
    assert src.count("mnesis_ingest") == 1


def test_daemon_routes_contradiction_to_review_without_forcing(tmp_path):
    f = tmp_path / "conflicting.txt"
    f.write_text("Atlas uses Memcached, not Redis.", encoding="utf-8")

    src = RecordingSource(responses={
        "mnesis_ingest": json.dumps({
            "action_taken": "contradict",
            "page_id": "p-new",
            "review_id": 7,
            "redaction_count": 0,
        }),
    })
    daemon = IngestDaemon(_registry(src))

    outcomes = run(daemon.scan_once(tmp_path))
    assert len(outcomes) == 1
    o = outcomes[0]
    assert o.status == "ingested"
    assert o.action == "contradict"
    assert o.review_id == 7

    # the daemon ingested once and did NOT force a resolution
    assert src.count("mnesis_ingest") == 1
    assert src.count("mnesis_resolve") == 0


def test_daemon_skips_malformed_file(tmp_path, caplog):
    bad = tmp_path / "empty.txt"
    bad.write_text("   \n  ", encoding="utf-8")  # whitespace only → empty
    src = RecordingSource()
    daemon = IngestDaemon(_registry(src))

    with caplog.at_level(logging.WARNING, logger="mnesis_agent.daemon"):
        outcomes = run(daemon.scan_once(tmp_path))

    assert len(outcomes) == 1
    assert outcomes[0].status == "skipped_malformed"
    assert src.count("mnesis_ingest") == 0          # never dispatched
    assert any("malformed" in r.message or "skip" in r.message for r in caplog.records)


def test_daemon_one_bad_file_does_not_block_others(tmp_path):
    (tmp_path / "good.txt").write_text("Atlas uses Redis.", encoding="utf-8")
    (tmp_path / "empty.md").write_text("", encoding="utf-8")  # malformed
    src = RecordingSource(responses={
        "mnesis_ingest": json.dumps({"action_taken": "new", "page_id": "p1", "redaction_count": 0}),
    })
    daemon = IngestDaemon(_registry(src))

    outcomes = run(daemon.scan_once(tmp_path))
    statuses = {o.source_ref: o.status for o in outcomes}
    assert statuses["good"] == "ingested"
    assert statuses["empty"] == "skipped_malformed"
    assert src.count("mnesis_ingest") == 1


def test_daemon_ingest_tool_error_is_logged_and_skipped(tmp_path, caplog):
    f = tmp_path / "note.txt"
    f.write_text("Atlas uses Redis.", encoding="utf-8")

    class FailingSource(ToolSource):
        async def list_tools(self):
            return [ToolSpec("mnesis_ingest", "Ingest", {"type": "object"})]
        async def call_tool(self, name, args):
            raise RuntimeError("mnesis down")

    daemon = IngestDaemon(_registry(FailingSource()))
    with caplog.at_level(logging.ERROR, logger="mnesis_agent.daemon"):
        outcome = run(daemon.process_file(f))

    assert outcome.status == "error"
    assert "mnesis down" in outcome.message
    # not marked seen → a transient failure can be retried later
    assert source_ref_for(f) not in daemon.seen


def test_daemon_only_watches_text_suffixes(tmp_path):
    (tmp_path / "doc.txt").write_text("text", encoding="utf-8")
    (tmp_path / "image.png").write_bytes(b"\x89PNG\r\n")
    (tmp_path / "data.json").write_text("{}", encoding="utf-8")
    src = RecordingSource(responses={
        "mnesis_ingest": json.dumps({"action_taken": "new", "page_id": "p1", "redaction_count": 0}),
    })
    daemon = IngestDaemon(_registry(src))
    outcomes = run(daemon.scan_once(tmp_path))
    refs = {o.source_ref for o in outcomes}
    assert refs == {"doc"}  # only the .txt file was considered


def test_daemon_watch_bounded_by_max_cycles(tmp_path):
    (tmp_path / "a.txt").write_text("Atlas uses Redis.", encoding="utf-8")
    src = RecordingSource(responses={
        "mnesis_ingest": json.dumps({"action_taken": "new", "page_id": "p1", "redaction_count": 0}),
    })
    daemon = IngestDaemon(_registry(src))
    seen: list = []
    outcomes = run(daemon.watch(
        tmp_path, poll_interval=0, max_cycles=2, on_outcome=seen.append
    ))
    # First cycle ingests; second finds nothing new (idempotent).
    assert src.count("mnesis_ingest") == 1
    assert len(outcomes) == 1
    assert seen == outcomes


# ── source_ref derivation ──────────────────────────────────────────────────────


def test_source_ref_slugifies_stem(tmp_path):
    assert source_ref_for(tmp_path / "My Notes 2026.txt") == "my-notes-2026"
    assert source_ref_for(tmp_path / "redis_cache.md") == "redis-cache"


# ── ingest-result parsing (JSON from fakes + real MCP-tool text) ──────────────


def test_parse_ingest_result_json_shape():
    raw = json.dumps({"action_taken": "contradict", "page_id": "p1", "review_id": 7})
    out = _parse_ingest_result(raw)
    assert out == {"action": "contradict", "page_id": "p1", "review_id": 7}


def test_parse_ingest_result_real_text_shape():
    # The real mnesis_ingest tool returns human-readable "key: value" lines.
    raw = "ingested page: atlas-redis\ntitle: Atlas uses Redis\ntags: project:atlas\naction: new\nredactions: 0"
    out = _parse_ingest_result(raw)
    assert out["page_id"] == "atlas-redis"
    assert out["action"] == "new"
    assert out["review_id"] is None


def test_parse_ingest_result_text_with_review():
    raw = "ingested page: p-new\naction: contradict\nredactions: 0\nreview: 12"
    out = _parse_ingest_result(raw)
    assert out["action"] == "contradict"
    assert out["review_id"] == 12


def test_parse_ingest_result_empty_or_garbage():
    assert _parse_ingest_result("") == {"action": None, "page_id": None, "review_id": None}
    assert _parse_ingest_result("no colons here")["page_id"] is None


def test_daemon_reports_action_from_real_text_output(tmp_path):
    """End-to-end of the parse fix: a text-returning ingest tool yields a populated outcome."""
    f = tmp_path / "note.txt"
    f.write_text("Atlas uses Redis.", encoding="utf-8")
    # FakeToolSource returning the REAL tool's text shape (not JSON).
    text_result = "ingested page: atlas-redis\ntitle: t\ntags: project:atlas\naction: new\nredactions: 0"
    src = RecordingSource(responses={"mnesis_ingest": text_result})
    daemon = IngestDaemon(_registry(src))
    outcomes = run(daemon.scan_once(tmp_path))
    assert outcomes[0].status == "ingested"
    assert outcomes[0].action == "new"
    assert outcomes[0].page_id == "atlas-redis"
