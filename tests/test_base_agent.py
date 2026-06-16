"""Tests for the F4 base agent + the three category ABCs.

Offline: a scripted stub model (F1) + fake Mnesis tools (F2) + the bundled
example skill (F3). No keys, no network, no running Mnesis.
"""
from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage

from mnesis_agents.base import Agent, AgentProfile, AgentResult, build_agent
from mnesis_agents.knowledge import FakeMnesisTools, ToolRegistry
from mnesis_agents.models import make_stub_model
from mnesis_agents.skills.loader import SkillRegistry
from mnesis_agents.categories import (
    ActionAgent,
    MaintenanceAgent,
    SmokeActionAgent,
    SmokeMaintenanceAgent,
    SmokeWritingAgent,
    WritingAgent,
)


def run(coro):
    return asyncio.run(coro)


def _mnesis_tools():
    return run(ToolRegistry([FakeMnesisTools()]).get_tools())


def _skills():
    return SkillRegistry().discover()


def _scripted_model(*messages: AIMessage):
    return make_stub_model(list(messages))


# ── base agent ──────────────────────────────────────────────────────────────


def test_build_agent_returns_runnable_agent():
    model = _scripted_model(AIMessage(content="hello"))
    agent = build_agent(AgentProfile(name="t", system_prompt="sys"), model=model)
    assert isinstance(agent, Agent)
    assert agent.graph is not None  # compiled LangGraph graph


def test_smoke_turn_calls_mnesis_tool_and_activates_skill():
    model = _scripted_model(
        AIMessage(content="", tool_calls=[{"name": "mnesis_query", "args": {"query": "redis"}, "id": "1"}]),
        AIMessage(content="", tool_calls=[{"name": "use_skill", "args": {"name": "summarize-source"}, "id": "2"}]),
        AIMessage(content="Atlas uses Redis for caching [atlas]."),
    )
    profile = AgentProfile(
        name="smoke", system_prompt="You are a smoke agent.",
        tools=_mnesis_tools(), skills=_skills(),
    )
    res = build_agent(profile, model=model).run("summarize the redis page")
    assert isinstance(res, AgentResult)
    assert res.output == "Atlas uses Redis for caching [atlas]."
    assert "mnesis_query" in res.tools_used
    assert "use_skill" in res.tools_used
    assert res.skills_used == ["summarize-source"]
    assert res.steps >= 3


def test_writes_are_tracked():
    model = _scripted_model(
        AIMessage(content="", tool_calls=[{"name": "mnesis_ingest", "args": {"text": "x", "source_ref": "s"}, "id": "1"}]),
        AIMessage(content="ingested."),
    )
    profile = AgentProfile(
        name="w", system_prompt="ingest things.", tools=_mnesis_tools(),
        write_tools=frozenset({"mnesis_ingest"}), write_policy="apply",  # F6: writes need an execute policy
    )
    res = build_agent(profile, model=model).run("ingest this")
    assert [w["tool"] for w in res.writes] == ["mnesis_ingest"]
    assert res.writes[0]["args_keys"] == ["source_ref", "text"]


def test_use_skill_tool_wired_when_skills_present():
    # With a skills registry, build_agent adds the use_skill tool (+ cards) so the
    # model can activate a skill; proven by scripting a use_skill call.
    model = _scripted_model(
        AIMessage(content="", tool_calls=[{"name": "use_skill", "args": {"name": "summarize-source"}, "id": "1"}]),
        AIMessage(content="ok"),
    )
    profile = AgentProfile(name="t", system_prompt="BASE_SYS", skills=_skills())
    res = build_agent(profile, model=model).run("hi")
    assert res.skills_used == ["summarize-source"] and res.output == "ok"


def test_async_run():
    model = _scripted_model(AIMessage(content="async-ok"))
    agent = build_agent(AgentProfile(name="t", system_prompt="s"), model=model)
    res = run(agent.arun("hi"))
    assert res.output == "async-ok"


# ── category ABCs: contract + enforcement ───────────────────────────────────


def test_category_trigger_and_write_policy_declared():
    assert WritingAgent.trigger == "event" and WritingAgent.write_policy == "ingest"
    assert ActionAgent.trigger == "event_or_schedule" and ActionAgent.write_policy == "propose"
    assert MaintenanceAgent.trigger == "schedule" and MaintenanceAgent.write_policy == "propose"


def test_incomplete_writing_subclass_cannot_instantiate():
    class Incomplete(WritingAgent):
        def system_prompt(self) -> str:
            return "s"
        # missing parse_artifact AND source_ref
    with pytest.raises(TypeError) as ei:
        Incomplete()
    assert "parse_artifact" in str(ei.value) or "source_ref" in str(ei.value)


def test_incomplete_action_subclass_cannot_instantiate():
    class Incomplete(ActionAgent):
        def system_prompt(self) -> str:
            return "s"
        # missing action_tools
    with pytest.raises(TypeError):
        Incomplete()


def test_incomplete_maintenance_subclass_cannot_instantiate():
    class Incomplete(MaintenanceAgent):
        def system_prompt(self) -> str:
            return "s"
        # missing cadence + scope
    with pytest.raises(TypeError):
        Incomplete()


# ── smoke example subclasses build + run end to end ─────────────────────────


def test_smoke_writing_agent_builds_and_runs():
    sw = SmokeWritingAgent(tools=_mnesis_tools(), skills=_skills(),
                           model=_scripted_model(AIMessage(content="ingested (smoke).")))
    assert sw.write_tools() == frozenset({"mnesis_ingest"})
    assert sw.parse_artifact("hello") == "hello" and sw.source_ref("hello") == "smoke-source"
    res = sw.build().run("ingest: hello")
    assert res.output == "ingested (smoke)."


def test_smoke_action_agent_includes_action_tool():
    sa = SmokeActionAgent(tools=_mnesis_tools(),
                          model=_scripted_model(AIMessage(content="acted (smoke).")))
    tool_names = {t.name for t in sa.tools()}
    assert "echo" in tool_names  # the action channel
    assert any(t.name.startswith("mnesis_") for t in sa.tools())  # plus Mnesis read tools
    res = sa.build().run("do the thing")
    assert res.output == "acted (smoke)."


def test_smoke_maintenance_agent_declares_cadence_and_scope():
    sm = SmokeMaintenanceAgent(tools=_mnesis_tools(),
                               model=_scripted_model(AIMessage(content="reviewed (smoke).")))
    assert sm.cadence() == "manual" and sm.scope() == ["smoke"]
    assert sm.trigger == "schedule"
    res = sm.build().run("run the dream cycle")
    assert res.output == "reviewed (smoke)."
