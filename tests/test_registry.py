"""Tests for ToolRegistry: aggregation, routing, error handling.

All tests run offline via FakeToolSource.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from mnesis_agent.fake_tools import FakeToolSource
from mnesis_agent.mcp_client import ToolSpec
from mnesis_agent.registry import ToolNotFoundError, ToolRegistry


def run(coro):
    return asyncio.run(coro)


# ── list_tools ────────────────────────────────────────────────────────────────


def test_list_tools_returns_all_from_single_source():
    reg = ToolRegistry()
    reg.add_source(FakeToolSource())
    tools = run(reg.list_tools())
    names = {t.name for t in tools}
    assert {"mnesis_query", "mnesis_get", "mnesis_ingest"} <= names


def test_list_tools_aggregates_multiple_sources():
    src_a = FakeToolSource(tools=[ToolSpec("tool_a", "A")], responses={"tool_a": "{}"})
    src_b = FakeToolSource(tools=[ToolSpec("tool_b", "B")], responses={"tool_b": "{}"})
    reg = ToolRegistry()
    reg.add_source(src_a)
    reg.add_source(src_b)
    names = {t.name for t in run(reg.list_tools())}
    assert "tool_a" in names and "tool_b" in names


def test_list_tools_returns_toolspec_instances():
    reg = ToolRegistry()
    reg.add_source(FakeToolSource())
    for t in run(reg.list_tools()):
        assert isinstance(t, ToolSpec)
        assert t.name and isinstance(t.description, str)
        assert isinstance(t.input_schema, dict)


def test_list_tools_empty_registry_returns_empty():
    reg = ToolRegistry()
    assert run(reg.list_tools()) == []


# ── refresh ───────────────────────────────────────────────────────────────────


def test_refresh_rebuilds_index():
    reg = ToolRegistry()
    reg.add_source(FakeToolSource())
    tools_a = run(reg.refresh())
    tools_b = run(reg.refresh())
    assert {t.name for t in tools_a} == {t.name for t in tools_b}


def test_refresh_picks_up_new_source():
    reg = ToolRegistry()
    reg.add_source(FakeToolSource(tools=[ToolSpec("tool_a", "A")], responses={"tool_a": "{}"}))
    run(reg.refresh())

    reg.add_source(FakeToolSource(tools=[ToolSpec("tool_b", "B")], responses={"tool_b": "{}"}))
    run(reg.refresh())

    result = run(reg.dispatch("tool_b", {}))
    assert result is not None


# ── dispatch ──────────────────────────────────────────────────────────────────


def test_dispatch_routes_to_correct_source():
    src_a = FakeToolSource(tools=[ToolSpec("tool_a", "A")], responses={"tool_a": '{"from": "a"}'})
    src_b = FakeToolSource(tools=[ToolSpec("tool_b", "B")], responses={"tool_b": '{"from": "b"}'})
    reg = ToolRegistry()
    reg.add_source(src_a)
    reg.add_source(src_b)
    run(reg.refresh())

    assert json.loads(run(reg.dispatch("tool_a", {})))["from"] == "a"
    assert json.loads(run(reg.dispatch("tool_b", {})))["from"] == "b"


def test_dispatch_auto_refreshes_on_first_call():
    # No explicit refresh — dispatch should build the index automatically.
    reg = ToolRegistry()
    reg.add_source(FakeToolSource())
    result = run(reg.dispatch("mnesis_query", {"query": "test"}))
    parsed = json.loads(result)
    assert "hits" in parsed


def test_dispatch_all_default_mnesis_tools():
    reg = ToolRegistry()
    reg.add_source(FakeToolSource())
    run(reg.refresh())
    for tool in ("mnesis_query", "mnesis_get", "mnesis_ingest", "mnesis_file_back", "mnesis_list"):
        result = run(reg.dispatch(tool, {}))
        assert result  # non-empty JSON string
        json.loads(result)  # valid JSON


def test_dispatch_passes_args_to_source():
    # FakeToolSource ignores args, but the call must reach it.
    received: list[dict] = []

    class SpySource(FakeToolSource):
        async def call_tool(self, name: str, args: dict) -> str:
            received.append(args)
            return await super().call_tool(name, args)

    reg = ToolRegistry()
    reg.add_source(SpySource())
    run(reg.refresh())
    run(reg.dispatch("mnesis_query", {"query": "redis", "limit": 5}))
    assert received == [{"query": "redis", "limit": 5}]


def test_dispatch_unknown_tool_raises():
    reg = ToolRegistry()
    reg.add_source(FakeToolSource())
    run(reg.refresh())
    with pytest.raises(ToolNotFoundError, match="no_such_tool"):
        run(reg.dispatch("no_such_tool", {}))


def test_dispatch_no_sources_raises():
    reg = ToolRegistry()
    with pytest.raises(ToolNotFoundError):
        run(reg.dispatch("anything", {}))


# ── spec ──────────────────────────────────────────────────────────────────────


def test_spec_returns_toolspec_after_refresh():
    reg = ToolRegistry()
    reg.add_source(FakeToolSource())
    run(reg.refresh())
    spec = reg.spec("mnesis_query")
    assert spec is not None
    assert spec.name == "mnesis_query"
    assert "query" in spec.input_schema.get("properties", {})


def test_spec_returns_none_before_refresh():
    reg = ToolRegistry()
    reg.add_source(FakeToolSource())
    assert reg.spec("mnesis_query") is None


# ── last-source-wins shadowing ────────────────────────────────────────────────


def test_later_source_shadows_earlier_for_same_tool_name():
    src_first = FakeToolSource(
        tools=[ToolSpec("shared", "first")],
        responses={"shared": '{"version": 1}'},
    )
    src_second = FakeToolSource(
        tools=[ToolSpec("shared", "second")],
        responses={"shared": '{"version": 2}'},
    )
    reg = ToolRegistry()
    reg.add_source(src_first)
    reg.add_source(src_second)
    run(reg.refresh())
    result = json.loads(run(reg.dispatch("shared", {})))
    assert result["version"] == 2
