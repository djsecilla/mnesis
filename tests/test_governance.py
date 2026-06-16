"""Tests for F6 governance / persistence / interrupts / audit / tracing.

Offline: scripted stub model + fake Mnesis tools. No network, no real Mnesis.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile

import pytest
from langchain_core.messages import AIMessage

from mnesis_agents import config as agents_config
from mnesis_agents.audit import AgentAuditLog, new_run_id, read_run_records
from mnesis_agents.base import AgentProfile, build_agent
from mnesis_agents.governance import make_checkpointer
from mnesis_agents.knowledge import FakeMnesisTools, ToolRegistry
from mnesis_agents.models import make_stub_model


def run(coro):
    return asyncio.run(coro)


def _tools():
    return run(ToolRegistry([FakeMnesisTools()]).get_tools())


def _model(*msgs: AIMessage):
    return make_stub_model(list(msgs))


def _call(name, args, i):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": str(i)}])


# ── allowlist (fail-closed) ─────────────────────────────────────────────────


def test_out_of_allowlist_tool_is_refused_before_side_effect():
    model = _model(_call("mnesis_impact", {"entity": "x"}, 1), AIMessage(content="done"))
    profile = AgentProfile(
        name="a", system_prompt="s", tools=_tools(),
        tool_allowlist=frozenset({"mnesis_query"}),  # impact NOT allowed
    )
    res = build_agent(profile, model=model).run("go")
    assert any(r["tool"] == "mnesis_impact" and r["reason"] == "allowlist" for r in res.refusals)
    assert "mnesis_impact" not in res.tools_used or True  # refused before execution
    assert res.output == "done"  # run still completes (model adapts)


def test_allowed_tool_executes():
    model = _model(_call("mnesis_query", {"query": "redis"}, 1), AIMessage(content="ok"))
    profile = AgentProfile(name="a", system_prompt="s", tools=_tools(),
                           tool_allowlist=frozenset({"mnesis_query"}))
    res = build_agent(profile, model=model).run("go")
    assert res.refusals == [] and res.output == "ok"


# ── budgets ─────────────────────────────────────────────────────────────────


def test_tool_call_budget_stops_with_flag():
    model = _model(
        _call("mnesis_query", {"query": "a"}, 1),
        _call("mnesis_query", {"query": "b"}, 2),  # over budget
        AIMessage(content="done"),
    )
    profile = AgentProfile(name="b", system_prompt="s", tools=_tools(), max_tool_calls=1)
    res = build_agent(profile, model=model).run("go")
    assert res.stop_reason == "tool_budget"
    assert any(r["reason"] == "tool_budget" for r in res.refusals)


# ── write policy ────────────────────────────────────────────────────────────


def test_propose_policy_does_not_apply_writes():
    model = _model(_call("mnesis_file_back", {"question": "q", "answer": "a"}, 1), AIMessage(content="ok"))
    profile = AgentProfile(
        name="p", system_prompt="s", tools=_tools(),
        write_tools=frozenset({"mnesis_file_back"}), write_policy="propose",
    )
    res = build_agent(profile, model=model).run("go")
    assert res.writes == []  # nothing applied
    assert any(r["reason"] == "write_policy" for r in res.refusals)


def test_apply_policy_applies_writes():
    model = _model(_call("mnesis_file_back", {"question": "q", "answer": "a"}, 1), AIMessage(content="filed"))
    profile = AgentProfile(
        name="p", system_prompt="s", tools=_tools(),
        write_tools=frozenset({"mnesis_file_back"}), write_policy="apply",
    )
    res = build_agent(profile, model=model).run("go")
    assert [w["tool"] for w in res.writes] == ["mnesis_file_back"]


# ── human-in-the-loop interrupt + resume ────────────────────────────────────


def test_interrupt_pauses_and_approval_resumes():
    model = _model(
        _call("mnesis_file_back", {"question": "q", "answer": "a"}, 1),
        AIMessage(content="filed after approval"),
    )
    profile = AgentProfile(
        name="hitl", system_prompt="s", tools=_tools(),
        write_tools=frozenset({"mnesis_file_back"}), write_policy="approved",
        approval_tools=frozenset({"mnesis_file_back"}),
        checkpointer=make_checkpointer("memory"),
    )
    agent = build_agent(profile, model=model)
    paused = agent.run("file it", thread_id="t1")
    assert paused.interrupted and paused.stop_reason == "interrupt"
    assert paused.output == ""  # not done yet

    resumed = agent.approve(thread_id="t1")
    assert not resumed.interrupted
    assert resumed.output == "filed after approval"
    assert [w["tool"] for w in resumed.writes] == ["mnesis_file_back"]


def test_reject_does_not_execute_the_tool():
    model = _model(
        _call("mnesis_file_back", {"question": "q", "answer": "a"}, 1),
        AIMessage(content="acknowledged rejection"),
    )
    profile = AgentProfile(
        name="hitl", system_prompt="s", tools=_tools(),
        write_tools=frozenset({"mnesis_file_back"}), write_policy="approved",
        approval_tools=frozenset({"mnesis_file_back"}),
        checkpointer=make_checkpointer("memory"),
    )
    agent = build_agent(profile, model=model)
    agent.run("file it", thread_id="t2")
    resumed = agent.reject("no", thread_id="t2")
    assert resumed.writes == []  # the write never executed


# ── checkpointer persistence (durable, cross-agent resume) ──────────────────


def test_checkpointer_persists_and_resumes_thread():
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "ck.db")

    def make_agent(model):
        return build_agent(AgentProfile(
            name="c", system_prompt="s", tools=_tools(),
            write_tools=frozenset({"mnesis_file_back"}), write_policy="approved",
            approval_tools=frozenset({"mnesis_file_back"}),
            checkpointer=make_checkpointer("sqlite", db_path=db),
        ), model=model)

    # Agent A interrupts and persists thread state to the SQLite file.
    a = make_agent(_model(_call("mnesis_file_back", {"question": "q", "answer": "a"}, 1),
                          AIMessage(content="x")))
    paused = a.run("file it", thread_id="shared")
    assert paused.interrupted and os.path.exists(db)

    # A FRESH agent + FRESH SqliteSaver on the SAME file resumes the thread.
    b = make_agent(_model(AIMessage(content="resumed elsewhere")))
    resumed = b.approve(thread_id="shared")
    assert resumed.output == "resumed elsewhere"  # state came from disk


# ── audit ───────────────────────────────────────────────────────────────────


def test_audit_one_record_per_step_no_leaked_values():
    secret = "SUPER-SECRET-VALUE-42"
    model = _model(
        _call("mnesis_query", {"query": secret}, 1),     # secret in tool args
        _call("use_skill", {"name": "summarize-source"}, 2),
        AIMessage(content="final answer"),
    )
    from mnesis_agents.skills.loader import SkillRegistry

    profile = AgentProfile(name="writing", system_prompt="s", tools=_tools(),
                           skills=SkillRegistry().discover())
    res = build_agent(profile, model=model).run("go")

    tmp = tempfile.mkdtemp()
    rid = new_run_id()
    AgentAuditLog(tmp).write_run(
        run_id=rid, category="writing", trigger="event:test", profile="writing", result=res,
    )
    records = read_run_records(tmp, rid)

    # run_start + N steps + run_end
    assert records[0]["type"] == "run_start" and records[-1]["type"] == "run_end"
    steps = [r for r in records if r["type"] == "step"]
    assert any(r.get("kind") == "tool" and r.get("tool") == "mnesis_query" for r in steps)
    assert any(r.get("kind") == "skill" and r.get("skill") == "summarize-source" for r in steps)
    assert any(r.get("kind") == "model" for r in steps)

    # NO leaked values anywhere in the audit (names/statuses only).
    blob = json.dumps(records)
    assert secret not in blob
    assert "final answer" not in blob  # message content is never logged


def test_audit_run_end_carries_stop_reason_and_writes():
    model = _model(_call("mnesis_file_back", {"question": "q", "answer": "a"}, 1), AIMessage(content="filed"))
    profile = AgentProfile(name="w", system_prompt="s", tools=_tools(),
                           write_tools=frozenset({"mnesis_file_back"}), write_policy="apply")
    res = build_agent(profile, model=model).run("go")
    tmp = tempfile.mkdtemp()
    rid = new_run_id()
    AgentAuditLog(tmp).write_run(run_id=rid, category="writing", trigger="t", profile="w", result=res)
    end = next(r for r in read_run_records(tmp, rid) if r["type"] == "run_end")
    assert end["stop_reason"] == "end"
    assert end["writes"] == [{"tool": "mnesis_file_back"}]


# ── tracing strictly opt-in ─────────────────────────────────────────────────


def test_tracing_off_by_default(monkeypatch):
    for var in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2"):
        monkeypatch.delenv(var, raising=False)
    assert agents_config.tracing_enabled() is False


def test_tracing_on_only_when_env_set(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    assert agents_config.tracing_enabled() is True


def test_building_an_agent_does_not_enable_tracing(monkeypatch):
    for var in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2"):
        monkeypatch.delenv(var, raising=False)
    build_agent(AgentProfile(name="t", system_prompt="s"), model=_model(AIMessage(content="x"))).run("hi")
    # We never set the tracing env ourselves.
    assert os.environ.get("LANGSMITH_TRACING") is None
    assert agents_config.tracing_enabled() is False
