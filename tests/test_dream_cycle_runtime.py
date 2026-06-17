"""Tests for M5: the dream cycle wired into the deployed runtime (F5 runner).

Offline/stub: fake Mnesis tools (F2) + bundled M2 skills (F3). Proves the runner
registers the scheduled maintenance agent, a fired cycle auto-applies safe
hygiene and queues proposals + writes a report, and that startup is resilient
(idle, never a crash) when Mnesis is unreachable or the dream cycle is disabled.
"""
from __future__ import annotations

import asyncio

from mnesis_agents import cli, config
from mnesis_agents.knowledge import FakeMaintenanceTools, FakeMnesisTools, ToolRegistry
from mnesis_agents.proposals import ProposalStore
from mnesis_agents.registry import AgentRegistry
from mnesis_agents.reports import DreamReportStore
from mnesis_agents.runner import Runner
from mnesis_agents.triggers.schedule import Schedule


def _tools():
    return asyncio.run(ToolRegistry([FakeMaintenanceTools(), FakeMnesisTools()]).get_tools())


# ── runner schedule resolution ──────────────────────────────────────────────


def test_runner_schedule_is_interval_based(monkeypatch):
    # The bundled scheduler is interval-only; the runner derives an interval.
    monkeypatch.setattr(config, "MNESIS_AGENTS_DREAM_INTERVAL_SECONDS", None)
    assert cli._runner_dream_schedule().interval_seconds == 86400.0  # ~daily/nightly
    monkeypatch.setattr(config, "MNESIS_AGENTS_DREAM_INTERVAL_SECONDS", 30.0)
    assert cli._runner_dream_schedule().interval_seconds == 30.0


# ── registration + firing (stub end-to-end) ─────────────────────────────────


def test_registered_dream_cycle_fires_and_does_the_work(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MNESIS_AGENTS_PROPOSALS_DIR", tmp_path)

    registry = AgentRegistry()
    sub = cli.register_maintenance_agent(
        registry, tools=_tools(), schedule=Schedule(interval_seconds=0.05)
    )
    assert sub.name == "dream-cycle"
    assert registry.schedule_subs and not registry.event_subs  # scheduled, single owner

    runner = Runner(registry)

    def fired_ok() -> bool:
        return any(r.subscription == "dream-cycle" and r.status == "ok" for r in runner.records)

    async def go():
        await runner.start()
        for _ in range(80):
            await asyncio.sleep(0.05)
            if fired_ok():
                break
        await runner.stop()

    asyncio.run(go())

    assert fired_ok()
    report = DreamReportStore(tmp_path).latest()
    assert report is not None
    # Auto-applied safe hygiene (decay + safe graph fixes).
    assert report["totals"]["auto_applied"] == 2
    assert "mnesis_resolve" not in report["totals"]["tools_called"]  # proposals NOT applied
    # Contradiction + dedup proposals queued for review.
    assert report["totals"]["proposals"] == 2
    assert len(ProposalStore(tmp_path).list_open()) == 2


# ── resilient startup ───────────────────────────────────────────────────────


def test_build_runner_registers_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MNESIS_AGENTS_DREAM_ENABLED", True)
    monkeypatch.setattr(config, "MNESIS_AGENTS_PROPOSALS_DIR", tmp_path)
    monkeypatch.setattr(cli, "_load_mcp_tools", _tools)
    runner = cli._build_runner()
    assert not runner.registry.is_empty
    assert runner.registry.schedule_subs[0].name == "dream-cycle"


def test_build_runner_idle_when_mcp_unreachable(monkeypatch):
    monkeypatch.setattr(config, "MNESIS_AGENTS_DREAM_ENABLED", True)

    def boom():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(cli, "_load_mcp_tools", boom)
    runner = cli._build_runner()
    assert runner.registry.is_empty  # resilient: idle, no crash


def test_build_runner_idle_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "MNESIS_AGENTS_DREAM_ENABLED", False)
    calls = {"n": 0}

    def spy():
        calls["n"] += 1
        return _tools()

    monkeypatch.setattr(cli, "_load_mcp_tools", spy)
    runner = cli._build_runner()
    assert runner.registry.is_empty and calls["n"] == 0  # not even loaded
