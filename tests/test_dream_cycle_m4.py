"""Tests for M4: dream-cycle proposals, reporting, crystallization, schedule.

Offline: fake Mnesis maintenance + read/write tools (F2) + bundled M2 skills (F3).
No keys, no network, no running Mnesis. Stores are pointed at a tmp dir.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

from mnesis_agents import config
from mnesis_agents.knowledge import FakeMaintenanceTools, FakeMnesisTools, ToolRegistry
from mnesis_agents.maintenance_agent import (
    DreamMaintenanceAgent,
    default_dream_schedule,
    register_dream_cycle,
)
from mnesis_agents.proposals import ProposalStore
from mnesis_agents.reports import DreamReportStore
from mnesis_agents.skills.loader import SkillRegistry


def _tools():
    # Maintenance tools (cycle) + read/write tools (crystallization + mnesis_get).
    return asyncio.run(ToolRegistry([FakeMaintenanceTools(), FakeMnesisTools()]).get_tools())


def _agent(tmp_path, **kw) -> DreamMaintenanceAgent:
    return DreamMaintenanceAgent(
        tools=_tools(),
        skills=SkillRegistry().discover(),
        proposal_store=ProposalStore(tmp_path),
        report_store=DreamReportStore(tmp_path),
        **kw,
    )


# ── proposals surface (never applied) ───────────────────────────────────────


def test_proposals_land_in_queue_and_are_not_applied(tmp_path):
    agent = _agent(tmp_path)
    report = agent.run_and_record()

    props = agent.proposals.list_open()
    kinds = {p.kind for p in props}
    assert kinds == {"contradiction", "duplicate"}

    # The contradiction proposal ANNOTATES the existing review by id (links to
    # review_id), recommending a keep — but never resolves it.
    contra = next(p for p in props if p.kind == "contradiction")
    assert contra.detail["review_id"] == 1
    assert contra.detail["keep"] == "atlas-redis" and contra.detail["supersede"] == "atlas-memcached"

    dup = next(p for p in props if p.kind == "duplicate")
    assert {dup.detail["page_a"], dup.detail["page_b"]} == {"atlas-redis", "atlas-redis-cache"}

    # Nothing knowledge-changing was applied.
    called = set(report.totals["tools_called"])
    assert "mnesis_resolve" not in called and "mnesis_ingest" not in called


def test_proposal_status_can_be_actioned_by_a_human(tmp_path):
    agent = _agent(tmp_path)
    agent.run_and_record()
    pid = agent.proposals.list_open()[0].id
    agent.proposals.set_status(pid, "dismissed")
    # A re-run does not resurrect a human-actioned proposal.
    agent.run_and_record()
    assert agent.proposals.get(pid).status == "dismissed"


# ── reporting (persist + retrieve + audit) ──────────────────────────────────


def test_report_persisted_and_retrievable(tmp_path):
    agent = _agent(tmp_path)
    agent.run_and_record()

    store = DreamReportStore(tmp_path)
    latest = store.latest()
    assert latest is not None and latest["totals"]["passes"] == 5
    summary = store.latest_summary()
    assert summary and "Dream cycle" in summary and "proposals:" in summary

    # Mirrored into the F6 audit log (counts/ids only).
    day_files = [f for f in os.listdir(tmp_path) if f.startswith("runs-")]
    assert day_files
    records = []
    for f in day_files:
        with open(tmp_path / f, encoding="utf-8") as fh:
            records += [json.loads(line) for line in fh if line.strip()]
    dream = [r for r in records if r.get("type") == "dream_cycle"]
    assert dream and dream[0]["totals"]["passes"] == 5
    assert {p["name"] for p in dream[0]["passes"]} >= {"decay-sweep", "deduplication"}


# ── crystallization (meta-memory; default off) ──────────────────────────────


def test_no_crystallization_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MNESIS_AGENTS_CRYSTALLIZE", False)
    report = _agent(tmp_path).run_and_record()
    assert "crystallized_digest_id" not in report.totals


def test_crystallization_files_a_maintenance_digest(tmp_path):
    agent = _agent(tmp_path, crystallize=True)
    report = agent.run_and_record()

    digest_id = report.totals.get("crystallized_digest_id")
    assert digest_id == "stub-digest"  # filed via fake mnesis_file_back

    # The filed digest is visible via fake mnesis_get.
    tools = {t.name: t for t in _tools()}
    page = json.loads(tools["mnesis_get"].invoke({"page_id": digest_id}))
    assert page["id"] == digest_id

    # The audit/report note the crystallized digest.
    assert DreamReportStore(tmp_path).latest()["totals"]["crystallized_digest_id"] == digest_id


def test_crystallization_noop_without_a_write_tool(tmp_path):
    # Only maintenance tools (no file_back/ingest) → nothing to crystallize with.
    maint = asyncio.run(ToolRegistry([FakeMaintenanceTools()]).get_tools())
    agent = DreamMaintenanceAgent(
        tools=maint, skills=SkillRegistry().discover(),
        proposal_store=ProposalStore(tmp_path), report_store=DreamReportStore(tmp_path),
        crystallize=True,
    )
    report = agent.run_and_record()
    assert "crystallized_digest_id" not in report.totals  # graceful no-op


# ── idempotency ─────────────────────────────────────────────────────────────


def test_repeated_runs_are_idempotent(tmp_path):
    agent = _agent(tmp_path)
    agent.run_and_record()
    n1 = len(agent.proposals.all())
    agent.run_and_record()
    agent.run_and_record()
    n2 = len(agent.proposals.all())
    assert n1 == n2 == 2  # 1 contradiction + 1 duplicate; no piling up


# ── schedule (F5) + on-demand ───────────────────────────────────────────────


def test_default_schedule_is_nightly_cron(monkeypatch):
    monkeypatch.setattr(config, "MNESIS_AGENTS_DREAM_INTERVAL_SECONDS", None)
    monkeypatch.setattr(config, "MNESIS_AGENTS_DREAM_CRON", "0 3 * * *")
    assert default_dream_schedule().cron == "0 3 * * *"


def test_scheduled_trigger_fires_a_cycle(tmp_path):
    from mnesis_agents.registry import AgentRegistry
    from mnesis_agents.runner import Runner
    from mnesis_agents.triggers.schedule import Schedule

    agent = _agent(tmp_path)
    registry = AgentRegistry()
    sub = register_dream_cycle(registry, agent, schedule=Schedule(interval_seconds=0.05))
    assert sub.name == "dream-cycle"

    runner = Runner(registry)

    def fired_ok() -> bool:
        return any(r.subscription == "dream-cycle" and r.status == "ok" for r in runner.records)

    async def go():
        await runner.start()
        for _ in range(80):  # poll until the first cycle fires + records (robust to timing)
            await asyncio.sleep(0.05)
            if fired_ok():
                break
        await runner.stop()

    asyncio.run(go())

    assert fired_ok()
    assert DreamReportStore(tmp_path).latest() is not None
    # Idempotent across the repeated firings.
    assert len(agent.proposals.all()) == 2


def test_cli_now_runs_then_report_shows_latest(tmp_path, monkeypatch, capsys):
    from mnesis_agents import cli

    monkeypatch.setattr(config, "MNESIS_AGENTS_PROPOSALS_DIR", tmp_path)

    def fake_build(plan=None, crystallize=None):
        return DreamMaintenanceAgent(
            tools=_tools(), skills=SkillRegistry().discover(), plan=plan, crystallize=crystallize
        )

    monkeypatch.setattr(cli, "_build_dream_agent", fake_build)

    assert cli.main(["dream-cycle", "--now"]) == 0
    out = capsys.readouterr().out
    assert "Dream cycle" in out and "passes: 5" in out

    # --report prints the persisted latest summary.
    assert cli.main(["dream-cycle", "--report"]) == 0
    assert "Dream cycle" in capsys.readouterr().out


def test_cli_report_when_nothing_ran(tmp_path, monkeypatch, capsys):
    from mnesis_agents import cli

    monkeypatch.setattr(config, "MNESIS_AGENTS_PROPOSALS_DIR", tmp_path)
    assert cli.main(["dream-cycle", "--report"]) == 0
    assert "No dream cycle has run yet" in capsys.readouterr().out
