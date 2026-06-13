"""Tests for ToolSpec, FakeToolSource, and MCPToolSource error handling.

All tests run offline — no network, no running Mnesis.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from mnesis_agent.fake_tools import FakeToolSource
from mnesis_agent.mcp_client import MCPConnectionError, MCPToolError, ToolSpec


def run(coro):
    return asyncio.run(coro)


# ── ToolSpec ─────────────────────────────────────────────────────────────────


def test_toolspec_fields():
    spec = ToolSpec(name="foo", description="bar", input_schema={"type": "object"})
    assert spec.name == "foo"
    assert spec.description == "bar"
    assert spec.input_schema == {"type": "object"}


def test_toolspec_input_schema_defaults_empty():
    spec = ToolSpec(name="foo", description="bar")
    assert spec.input_schema == {}


# ── FakeToolSource — list_tools ──────────────────────────────────────────────


def test_fake_list_tools_returns_toolspecs():
    source = FakeToolSource()
    tools = run(source.list_tools())
    assert len(tools) >= 3
    for spec in tools:
        assert isinstance(spec, ToolSpec)
        assert spec.name
        assert isinstance(spec.description, str)
        assert isinstance(spec.input_schema, dict)


def test_fake_list_tools_includes_core_mnesis_tools():
    source = FakeToolSource()
    names = {t.name for t in run(source.list_tools())}
    assert {"mnesis_query", "mnesis_get", "mnesis_ingest"} <= names


def test_fake_list_tools_input_schemas_are_valid_json_schema():
    source = FakeToolSource()
    for spec in run(source.list_tools()):
        schema = spec.input_schema
        assert isinstance(schema, dict)
        if schema:
            assert schema.get("type") == "object"
            assert isinstance(schema.get("properties", {}), dict)


# ── FakeToolSource — call_tool ───────────────────────────────────────────────


def test_fake_call_mnesis_query_returns_hits():
    source = FakeToolSource()
    raw = run(source.call_tool("mnesis_query", {"query": "redis"}))
    parsed = json.loads(raw)
    assert "hits" in parsed
    assert isinstance(parsed["hits"], list)
    assert parsed["hits"][0]["id"]


def test_fake_call_mnesis_get_returns_page():
    source = FakeToolSource()
    raw = run(source.call_tool("mnesis_get", {"id": "atlas"}))
    parsed = json.loads(raw)
    assert "id" in parsed and "title" in parsed and "body" in parsed


def test_fake_call_mnesis_ingest_returns_result():
    source = FakeToolSource()
    raw = run(source.call_tool("mnesis_ingest", {"text": "some text"}))
    parsed = json.loads(raw)
    assert "action_taken" in parsed and "page_id" in parsed
    assert isinstance(parsed["redaction_count"], int)


def test_fake_call_unknown_tool_raises_mcp_tool_error():
    source = FakeToolSource()
    with pytest.raises(MCPToolError, match="unknown tool"):
        run(source.call_tool("no_such_tool", {}))


def test_fake_custom_tools_and_responses():
    custom_tools = [ToolSpec(name="my_tool", description="custom", input_schema={})]
    custom_responses = {"my_tool": '{"ok": true}'}
    source = FakeToolSource(tools=custom_tools, responses=custom_responses)

    tools = run(source.list_tools())
    assert len(tools) == 1 and tools[0].name == "my_tool"

    result = run(source.call_tool("my_tool", {}))
    assert json.loads(result) == {"ok": True}


def test_fake_custom_responses_merged_with_defaults():
    # Extra response is merged; default mnesis_query still works.
    source = FakeToolSource(responses={"custom": '{"x": 1}'})
    run(source.call_tool("mnesis_query", {"query": "test"}))  # default still present


def test_fake_list_tools_returns_independent_copy():
    # Mutations to the returned list don't affect the source's internal list.
    source = FakeToolSource()
    first = run(source.list_tools())
    first.clear()
    second = run(source.list_tools())
    assert len(second) >= 3


# ── MCPToolSource — connection error ─────────────────────────────────────────


def test_mcp_source_connection_error_is_clear():
    from mnesis_agent.mcp_client import MCPToolSource

    # Port 19999 is unlikely to be occupied; connection refused -> MCPConnectionError.
    source = MCPToolSource("http://localhost:19999/mcp")
    with pytest.raises(MCPConnectionError, match="Cannot reach"):
        run(source.list_tools())


def test_mcp_source_connection_error_call_tool():
    from mnesis_agent.mcp_client import MCPToolSource

    source = MCPToolSource("http://localhost:19999/mcp")
    with pytest.raises(MCPConnectionError, match="Cannot reach"):
        run(source.call_tool("mnesis_query", {"query": "test"}))
