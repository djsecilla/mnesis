"""Tests for the F5 triggers / registry / runner scaffold.

Offline: a stub-model smoke agent + in-memory event trigger + fast interval
schedule. No network, no real connectors.
"""
from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage

from mnesis_agents.base import AgentProfile, build_agent
from mnesis_agents.cli import build_parser
from mnesis_agents.models import make_stub_model
from mnesis_agents.registry import AgentRegistry
from mnesis_agents.runner import RunRecord, Runner
from mnesis_agents.triggers.events import InboundEvent, InMemoryEventTrigger
from mnesis_agents.triggers.schedule import (
    AsyncIntervalScheduler,
    Schedule,
    ScheduleTrigger,
)


def run(coro):
    return asyncio.run(coro)


def _smoke_agent(reply: str = "ran"):
    return build_agent(
        AgentProfile(name="smoke", system_prompt="s"),
        model=make_stub_model([AIMessage(content=reply)]),
    )


async def _settle(check, *, timeout=1.0, step=0.02):
    """Poll until ``check()`` is true or timeout (keeps schedule tests non-flaky)."""
    waited = 0.0
    while waited < timeout:
        if check():
            return True
        await asyncio.sleep(step)
        waited += step
    return check()


# ── event dispatch ──────────────────────────────────────────────────────────


def test_agent_runs_on_emitted_event():
    async def scenario():
        agent = _smoke_agent("event-handled")
        reg = AgentRegistry()
        reg.register_agent_on_event(agent, "on-note", source="notes")
        trig = InMemoryEventTrigger("notes")
        runner = Runner(reg, event_triggers=[trig])
        await runner.start()
        await trig.emit(InboundEvent(source="notes", kind="added", payload="hi", id="e1"))
        ok = await _settle(lambda: any(r.subscription == "on-note" for r in runner.records))
        await runner.stop()
        return runner, ok

    runner, ok = run(scenario())
    assert ok
    rec = next(r for r in runner.records if r.subscription == "on-note")
    assert rec.status == "ok" and rec.trigger == "event:notes/added"


def test_event_filter_skips_non_matching_source():
    async def scenario():
        agent = _smoke_agent()
        reg = AgentRegistry()
        reg.register_agent_on_event(agent, "only-email", source="email")
        trig = InMemoryEventTrigger("notes")
        runner = Runner(reg, event_triggers=[trig])
        await runner.start()
        await trig.emit(InboundEvent(source="notes", kind="added", payload="x"))
        await asyncio.sleep(0.1)
        await runner.stop()
        return runner

    runner = run(scenario())
    assert runner.records == []  # source mismatch -> no dispatch


def test_ack_hook_fires_after_dispatch():
    async def scenario():
        reg = AgentRegistry()
        reg.register_agent_on_event(_smoke_agent(), "on-note", source="notes")
        trig = InMemoryEventTrigger("notes")
        runner = Runner(reg, event_triggers=[trig])
        await runner.start()
        await trig.emit(InboundEvent(source="notes", kind="added", payload="x", id="e7"))
        await _settle(lambda: bool(trig.acked))
        await runner.stop()
        return trig

    trig = run(scenario())
    assert [e.id for e in trig.acked] == ["e7"]


# ── schedule dispatch ───────────────────────────────────────────────────────


def test_agent_runs_on_schedule_tick():
    async def scenario():
        reg = AgentRegistry()
        reg.register_agent_on_schedule(_smoke_agent("tick"), "dream", Schedule(interval_seconds=0.03))
        runner = Runner(reg)
        await runner.start()
        ok = await _settle(lambda: any(r.subscription == "dream" for r in runner.records))
        await runner.stop()
        return runner, ok

    runner, ok = run(scenario())
    assert ok
    assert all(r.status == "ok" for r in runner.records if r.subscription == "dream")


# ── resilience ──────────────────────────────────────────────────────────────


def test_raising_handler_is_caught_and_runner_continues():
    async def scenario():
        reg = AgentRegistry()

        async def boom(event):
            raise RuntimeError("kaboom")

        ran_after = {"n": 0}

        async def ok_handler(event):
            ran_after["n"] += 1

        reg.on_event("bad", boom, source="notes")
        reg.on_event("good", ok_handler, source="notes")
        trig = InMemoryEventTrigger("notes")
        runner = Runner(reg, event_triggers=[trig])
        await runner.start()
        # First event triggers both subs; the bad one must not stop the good one,
        # nor the runner — a second event must still be processed.
        await trig.emit(InboundEvent(source="notes", kind="x", payload="1"))
        await trig.emit(InboundEvent(source="notes", kind="x", payload="2"))
        await _settle(lambda: ran_after["n"] >= 2)
        await runner.stop()
        return runner, ran_after

    runner, ran_after = run(scenario())
    errors = [r for r in runner.records if r.status == "error"]
    assert errors and "kaboom" in errors[0].error  # caught + recorded
    assert ran_after["n"] >= 2  # good handler kept running across two events


def test_observer_receives_run_records():
    async def scenario():
        seen: list[RunRecord] = []
        reg = AgentRegistry()
        reg.register_agent_on_event(_smoke_agent(), "on-note", source="notes")
        trig = InMemoryEventTrigger("notes")
        runner = Runner(reg, event_triggers=[trig], observer=seen.append)
        await runner.start()
        await trig.emit(InboundEvent(source="notes", kind="x", payload="x"))
        await _settle(lambda: bool(seen))
        await runner.stop()
        return seen

    seen = run(scenario())
    assert seen and isinstance(seen[0], RunRecord)


# ── idle runner ─────────────────────────────────────────────────────────────


def test_idle_runner_starts_and_stops_cleanly():
    async def scenario():
        runner = Runner(AgentRegistry())
        assert runner.registry.is_empty
        await runner.start()
        await runner.start()  # idempotent
        await runner.stop()
        await runner.stop()   # idempotent
        return runner

    runner = run(scenario())
    assert runner.records == []


# ── schedule shape + scheduler guards ───────────────────────────────────────


def test_schedule_requires_interval_or_cron():
    with pytest.raises(ValueError):
        Schedule()


def test_interval_scheduler_rejects_cron():
    sched = AsyncIntervalScheduler()
    with pytest.raises(ValueError, match="APScheduler"):
        sched.add_job("c", Schedule(cron="* * * * *"), lambda: asyncio.sleep(0))


def test_schedule_trigger_is_abstract():
    with pytest.raises(TypeError):
        ScheduleTrigger()  # abstract: schedule() not implemented


# ── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_run_subcommand_parses():
    args = build_parser().parse_args(["run"])
    assert getattr(args, "func", None) is not None
