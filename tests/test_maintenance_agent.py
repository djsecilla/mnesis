"""Tests for the concrete dream-cycle MaintenanceAgent (M3).

Offline: a scripted stub model (F1) + the fake Mnesis maintenance tools (F2) +
the bundled M2 maintenance skills (F3). No keys, no network, no running Mnesis.

Validates that a dream cycle runs all passes, auto-applies decay + safe graph
fixes (observable as tool calls), accumulates contradiction/dedup proposals
WITHOUT resolving/applying, returns a populated report with health_before/after,
records a deliberately failing pass while completing, and honours budget caps.
"""
from __future__ import annotations

import asyncio

from langchain_core.messages import AIMessage

from mnesis_agents.categories import MaintenanceAgent
from mnesis_agents.knowledge import FakeMaintenanceTools, ToolRegistry
from mnesis_agents.maintenance_agent import DEFAULT_PLAN, DreamMaintenanceAgent
from mnesis_agents.models import make_stub_model
from mnesis_agents.skills.loader import SkillRegistry


def _tools():
    return asyncio.run(ToolRegistry([FakeMaintenanceTools()]).get_tools())


def _skills():
    return SkillRegistry().discover()


def _agent(**kw) -> DreamMaintenanceAgent:
    return DreamMaintenanceAgent(tools=_tools(), skills=_skills(), **kw)


# ── F4 contract ─────────────────────────────────────────────────────────────


def test_is_a_scheduled_maintenance_agent():
    a = _agent()
    assert isinstance(a, MaintenanceAgent)
    assert a.trigger == "schedule" and a.write_policy == "propose"
    assert a.cadence()  # a cron/interval string
    assert a.scope() == list(DEFAULT_PLAN)
    # Knowledge-changing writes are the proposal-only set; safe writers excluded.
    assert "mnesis_resolve" in a.write_tools()
    assert "mnesis_decay" not in a.write_tools()


def test_base_agent_still_builds_and_runs():
    # The dream-cycle orchestrator is deterministic, but the F4 base agent (an LLM
    # loop) is still available for ad-hoc maintenance chat.
    a = DreamMaintenanceAgent(
        tools=_tools(), skills=_skills(), model=make_stub_model([AIMessage(content="ok")])
    )
    assert a.build().run("status?").output == "ok"


# ── full dream cycle ────────────────────────────────────────────────────────


def test_full_cycle_runs_all_passes_with_health_framing():
    report = _agent().run_dream_cycle()

    assert [p.name for p in report.passes] == list(DEFAULT_PLAN)
    assert all(p.status == "ok" for p in report.passes), [p.error for p in report.passes]

    # Health framing captured before and after.
    assert report.health_before and report.health_before["pages_total"] == 7
    assert report.health_after and report.health_after["pages_total"] == 7
    assert report.started and report.ended

    totals = report.totals
    assert totals["passes"] == 5 and totals["ok"] == 5 and totals["failed"] == 0
    assert totals["stop_reason"] is None


def test_safe_hygiene_auto_applied_as_tool_calls():
    report = _agent().run_dream_cycle()
    by_name = {p.name: p for p in report.passes}

    # decay-sweep and graph-hygiene mark their mutating calls as auto-applied.
    assert by_name["decay-sweep"].summary["action"] == "auto_applied"
    assert [a["tool"] for a in by_name["decay-sweep"].auto_applied] == ["mnesis_decay"]
    assert by_name["graph-hygiene"].summary["action"] == "auto_applied"
    assert [a["tool"] for a in by_name["graph-hygiene"].auto_applied] == ["mnesis_graph_lint"]

    # Observable as actual governed tool calls.
    called = report.totals["tools_called"]
    assert "mnesis_decay" in called and "mnesis_graph_lint" in called
    assert report.totals["auto_applied"] == 2


def test_proposals_accumulated_without_resolving_or_applying():
    report = _agent().run_dream_cycle()
    by_name = {p.name: p for p in report.passes}

    triage = by_name["contradiction-triage"]
    assert triage.summary["action"] == "propose"
    assert len(triage.proposals) == 1
    assert triage.proposals[0]["keep"] == "atlas-redis"  # by confidence/sources/recency
    assert triage.auto_applied == []

    dedup = by_name["deduplication"]
    assert dedup.summary["action"] == "propose"
    assert len(dedup.proposals) == 1  # only the strong (0.62) pair
    assert dedup.auto_applied == []

    # NOTHING knowledge-changing was applied: no write tool was ever dispatched.
    called = set(report.totals["tools_called"])
    assert "mnesis_resolve" not in called
    assert "mnesis_ingest" not in called and "mnesis_file_back" not in called
    assert report.totals["proposals"] == 2


def test_quality_sweep_is_read_only_findings():
    report = _agent().run_dream_cycle()
    quality = next(p for p in report.passes if p.name == "quality-sweep")
    assert quality.summary["action"] == "report"
    assert quality.proposals == [] and quality.auto_applied == []
    types = {f["type"] for f in quality.summary["findings"]}
    assert "no_source_pages" in types


# ── resilience ──────────────────────────────────────────────────────────────


def test_failing_pass_is_recorded_and_cycle_completes():
    plan = ["decay-sweep", "bogus-pass", "deduplication"]
    report = _agent().run_dream_cycle(plan)

    assert [p.name for p in report.passes] == plan  # every pass node ran
    by_name = {p.name: p for p in report.passes}
    assert by_name["bogus-pass"].status == "failed" and by_name["bogus-pass"].error
    # The passes around the failure still succeeded.
    assert by_name["decay-sweep"].status == "ok"
    assert by_name["deduplication"].status == "ok"
    assert report.totals["failed"] == 1 and report.totals["ok"] == 2


# ── budgets ─────────────────────────────────────────────────────────────────


def test_budget_caps_are_honoured():
    # A tiny tool-call budget trips mid-cycle: later passes are skipped, the cycle
    # still completes with a populated report and a flagged stop reason.
    report = _agent(max_tool_calls=2).run_dream_cycle()

    assert report.totals["stop_reason"] == "tool_budget"
    assert report.totals["skipped"] >= 1
    assert report.totals["tool_calls"] <= 2
    # health_before fit in the budget; health_after did not.
    assert report.health_before is not None
    assert report.health_after is None
    # Every planned pass is still represented (resilient, no crash).
    assert [p.name for p in report.passes] == list(DEFAULT_PLAN)
