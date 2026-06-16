"""End-to-end smoke test of the F1–F6 foundation wired together (stub mode).

Builds an agent on the multi-LLM stub (F1), connects the fake Mnesis tools (F2),
activates the bundled example skill (F3), runs it through the runner (F5) under
governance (F6), and asserts a clean structured result + a clean audit record.
No network, no real Mnesis, no keys.
"""
from __future__ import annotations

import asyncio
import json
import tempfile

from langchain_core.messages import AIMessage

from mnesis_agents.audit import AgentAuditLog, new_run_id, read_run_records
from mnesis_agents.base import AgentProfile, build_agent
from mnesis_agents.knowledge import FakeMnesisTools, ToolRegistry
from mnesis_agents.models import make_stub_model
from mnesis_agents.registry import AgentRegistry
from mnesis_agents.runner import Runner
from mnesis_agents.skills.loader import SkillRegistry
from mnesis_agents.triggers.events import InboundEvent, InMemoryEventTrigger


def test_foundation_end_to_end_via_runner_with_governance_and_audit():
    async def scenario():
        # F2 tools + F3 skills.
        tools = await ToolRegistry([FakeMnesisTools()]).get_tools()
        skills = SkillRegistry().discover()

        # F1 stub model scripted for a full turn: read Mnesis → activate skill →
        # ingest (a governed write) → finish.
        model = make_stub_model([
            AIMessage(content="", tool_calls=[{"name": "mnesis_query", "args": {"query": "redis"}, "id": "1"}]),
            AIMessage(content="", tool_calls=[{"name": "use_skill", "args": {"name": "summarize-source"}, "id": "2"}]),
            AIMessage(content="", tool_calls=[{"name": "mnesis_ingest", "args": {"text": "t", "source_ref": "s"}, "id": "3"}]),
            AIMessage(content="Done — Atlas uses Redis [atlas]."),
        ])

        # F4 base agent + F6 governance (allowlist + write policy).
        profile = AgentProfile(
            name="writing-smoke",
            system_prompt="You are a writing smoke agent.",
            tools=tools,
            skills=skills,
            tool_allowlist=frozenset({"mnesis_query", "mnesis_ingest"}),
            write_tools=frozenset({"mnesis_ingest"}),
            write_policy="ingest",       # WritingAgent-style: ingest is an apply
        )
        agent = build_agent(profile, model=model)

        # F6 audit + a handler that runs the agent and records the run.
        audit = AgentAuditLog(tempfile.mkdtemp())
        run_ids: list[str] = []

        async def handler(event: InboundEvent):
            result = await agent.arun(str(event.payload))
            rid = new_run_id()
            run_ids.append(rid)
            audit.write_run(
                run_id=rid, category="writing", trigger=f"event:{event.source}/{event.kind}",
                profile=profile.name, result=result,
            )
            return result

        # F5 runner: event trigger → registry → dispatch.
        reg = AgentRegistry()
        reg.on_event("ingest-on-source", handler, source="notes")
        trig = InMemoryEventTrigger("notes")
        runner = Runner(reg, event_triggers=[trig])
        await runner.start()
        await trig.emit(InboundEvent(source="notes", kind="added", payload="a new note", id="e1"))

        # Wait for the dispatch to complete (one RunRecord).
        waited = 0.0
        while not runner.records and waited < 2.0:
            await asyncio.sleep(0.02)
            waited += 0.02
        await runner.stop()
        return runner, audit, run_ids

    runner, audit, run_ids = asyncio.run(scenario())

    # Runner dispatched cleanly.
    assert len(runner.records) == 1
    assert runner.records[0].status == "ok"
    assert runner.records[0].trigger == "event:notes/added"

    # Audit captured the run: run_start + steps (tool, skill, model) + run_end.
    records = read_run_records(audit.directory, run_ids[0])
    assert records[0]["type"] == "run_start" and records[-1]["type"] == "run_end"
    kinds = {r.get("kind") for r in records if r["type"] == "step"}
    assert {"tool", "skill", "model"} <= kinds
    end = records[-1]
    assert end["stop_reason"] == "end"
    assert "mnesis_query" in end["tools_used"]
    assert end["skills_used"] == ["summarize-source"]
    assert [w["tool"] for w in end["writes"]] == ["mnesis_ingest"]  # governed write applied

    # No leaked values anywhere in the audit.
    assert "a new note" not in json.dumps(records)
