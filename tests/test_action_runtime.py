"""Runtime wiring of the action agent into the F5 runner (A5). Offline/stub.

Proves the action-agent schedule hook registers and fires (composing proposal-only
briefs, delivering nothing), that `_build_runner` registers it only when enabled
and stays resilient when Mnesis is unreachable, and — structurally — that no
external-send channel exists (no external egress is possible in this set).
"""
from __future__ import annotations

import asyncio
import json

from langchain_core.tools import tool

from mnesis_agents import cli, config
from mnesis_agents.action_agent import GroundedActionAgent
from mnesis_agents.action_gate import ActionGate
from mnesis_agents.audit import AgentAuditLog
from mnesis_agents.channels import (
    RISK_INERT,
    ChannelRegistry,
    DraftOutboxChannel,
    LocalNotifyChannel,
    default_channel_registry,
)
from mnesis_agents.proposals import ActionProposalStore
from mnesis_agents.registry import AgentRegistry
from mnesis_agents.runner import Runner


def _query_tool():
    @tool
    def mnesis_query(query: str, limit: int = 10) -> str:
        """Search Mnesis."""
        return json.dumps({"query": query, "hits": [
            {"id": "atlas-redis", "title": "Atlas uses Redis", "snippet": "x", "status": "active"}]})

    return mnesis_query


def _agent(tmp_path):
    reg = ChannelRegistry([DraftOutboxChannel(tmp_path / "outbox"), LocalNotifyChannel(tmp_path / "n.jsonl")])
    gate = ActionGate(reg, store=ActionProposalStore(tmp_path), audit=AgentAuditLog(tmp_path))
    return GroundedActionAgent(tools=[_query_tool()], gate=gate, audit=AgentAuditLog(tmp_path))


def _drafts(tmp_path):
    return sorted((tmp_path / "outbox").glob("*.md"))


# ── only inert channels (no external egress is possible) ────────────────────


def test_default_channels_are_all_inert_no_external_send():
    reg = default_channel_registry()
    assert set(reg.names()) == {"draft-outbox", "local-notify"}
    assert all(reg.risk_class(n) == RISK_INERT for n in reg.names())  # nothing reaches a third party


# ── schedule hook registration + firing ─────────────────────────────────────


def test_register_action_agent_wires_a_schedule_hook(tmp_path):
    registry = AgentRegistry()
    agent = _agent(tmp_path)
    contexts = [{"topic": "Atlas caching"}, {"topic": "Auth migration"}]
    returned, sub = cli.register_action_agent(
        registry, agent=agent, contexts_provider=lambda: contexts,
    )
    assert returned is agent and sub.name == "action-schedule"
    assert registry.schedule_subs and not registry.event_subs

    results = asyncio.run(sub.handler())
    assert len(results) == 2 and all(r.status == "proposed" for r in results)
    # Proposals only — nothing delivered by the schedule.
    assert len(agent.gate.store.list_pending()) == 2 and _drafts(tmp_path) == []


def test_scheduled_hook_fires_via_the_runner(tmp_path):
    from mnesis_agents.triggers.schedule import Schedule

    registry = AgentRegistry()
    agent = _agent(tmp_path)
    cli.register_action_agent(
        registry, agent=agent, contexts_provider=lambda: [{"topic": "Atlas caching"}],
        schedule=Schedule(interval_seconds=0.05),
    )
    runner = Runner(registry)

    async def go():
        await runner.start()
        for _ in range(80):
            await asyncio.sleep(0.05)
            if agent.gate.store.list_pending():
                break
        await runner.stop()

    asyncio.run(go())
    assert len(agent.gate.store.list_pending()) == 1   # composed a proposal
    assert _drafts(tmp_path) == []                      # delivered nothing


# ── _build_runner gating + resilience ───────────────────────────────────────


def test_build_runner_registers_action_schedule_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MNESIS_AGENTS_DREAM_ENABLED", False)
    monkeypatch.setattr(config, "MNESIS_NOTES_ENABLED", False)
    monkeypatch.setattr(config, "MNESIS_AGENTS_ACTIONS_SCHEDULE_ENABLED", True)
    monkeypatch.setattr(config, "MNESIS_AGENTS_PROPOSALS_DIR", tmp_path)
    monkeypatch.setattr(config, "MNESIS_ACTION_OUTBOX", tmp_path / "outbox")
    monkeypatch.setattr(config, "MNESIS_AGENTS_CONNECTOR_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(cli, "_load_mcp_tools", lambda: [_query_tool()])

    runner = cli._build_runner()
    names = [s.name for s in runner.registry.schedule_subs]
    assert "action-schedule" in names


def test_build_runner_off_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MNESIS_AGENTS_DREAM_ENABLED", False)
    monkeypatch.setattr(config, "MNESIS_NOTES_ENABLED", False)
    monkeypatch.setattr(config, "MNESIS_AGENTS_ACTIONS_SCHEDULE_ENABLED", False)
    calls = {"n": 0}
    monkeypatch.setattr(cli, "_load_mcp_tools", lambda: calls.__setitem__("n", calls["n"] + 1) or [])
    runner = cli._build_runner()
    assert runner.registry.is_empty and calls["n"] == 0  # action schedule not registered


def test_build_runner_resilient_when_mcp_unreachable(monkeypatch):
    monkeypatch.setattr(config, "MNESIS_AGENTS_DREAM_ENABLED", False)
    monkeypatch.setattr(config, "MNESIS_NOTES_ENABLED", False)
    monkeypatch.setattr(config, "MNESIS_AGENTS_ACTIONS_SCHEDULE_ENABLED", True)

    def boom():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(cli, "_load_mcp_tools", boom)
    runner = cli._build_runner()   # must not crash
    assert runner.registry.is_empty
