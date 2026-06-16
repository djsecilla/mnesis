"""Tests for the Mnesis MCP tool source / registry.

Offline: the fake source needs no network. The real MultiServerMCPClient path is
exercised with a mocked client (transport stubbed), asserting the connection
config (URL + bearer header) and clear failure handling — never a live call.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from langchain_core.tools import BaseTool, tool

from mnesis_agents import config as agents_config
from mnesis_agents import knowledge
from mnesis_agents.knowledge import (
    FakeMnesisTools,
    MCPToolSource,
    MnesisConnectionError,
    MNESIS_TOOL_NAMES,
    ToolRegistry,
    ToolSource,
    mnesis_connection,
)


def run(coro):
    return asyncio.run(coro)


# ── fake source / registry ────────────────────────────────────────────────


def test_registry_exposes_mnesis_tools_as_langchain_tools():
    reg = ToolRegistry([FakeMnesisTools()])
    tools = run(reg.get_tools())
    assert all(isinstance(t, BaseTool) for t in tools)
    assert {t.name for t in tools} == set(MNESIS_TOOL_NAMES)


def test_tools_have_descriptions_and_schemas():
    tools = {t.name: t for t in run(ToolRegistry([FakeMnesisTools()]).get_tools())}
    q = tools["mnesis_query"]
    assert q.description  # non-empty
    props = q.args_schema.model_json_schema()["properties"]
    assert "query" in props and "limit" in props
    ing = tools["mnesis_ingest"]
    assert set(ing.args_schema.model_json_schema()["properties"]) >= {"text", "source_ref"}


def test_tool_invocation_routes_through_and_returns_result():
    tools = {t.name: t for t in run(ToolRegistry([FakeMnesisTools()]).get_tools())}
    out = tools["mnesis_query"].invoke({"query": "redis"})
    data = json.loads(out)
    assert data["query"] == "redis" and data["hits"][0]["id"] == "atlas"

    fb = json.loads(tools["mnesis_file_back"].invoke({"question": "Q?", "answer": "A."}))
    assert fb["filed"] is True and fb["digest_id"]


def test_async_tool_invocation():
    tools = {t.name: t for t in run(ToolRegistry([FakeMnesisTools()]).get_tools())}
    out = run(tools["mnesis_impact"].ainvoke({"entity": "library:redis"}))
    assert json.loads(out)["entity"] == "library:redis"


# ── namespacing ────────────────────────────────────────────────────────────


class _OtherSource(ToolSource):
    namespace = "other"

    async def load_tools(self):
        @tool
        def mnesis_query(query: str) -> str:  # deliberately collides with Mnesis
            """A different mnesis_query from another source."""
            return "other"

        @tool
        def web_search(q: str) -> str:
            """Unique tool, should keep its bare name."""
            return "web"

        return [mnesis_query, web_search]


def test_namespacing_only_on_collision():
    reg = ToolRegistry([FakeMnesisTools(), _OtherSource()])
    names = {t.name for t in run(reg.get_tools())}
    # The colliding mnesis_query is namespaced for BOTH sources…
    assert "mnesis__mnesis_query" in names
    assert "other__mnesis_query" in names
    assert "mnesis_query" not in names
    # …while non-colliding tools keep clean, bare names.
    assert "mnesis_get" in names
    assert "web_search" in names


def test_force_namespace_prefixes_everything():
    names = {t.name for t in run(ToolRegistry([FakeMnesisTools()]).get_tools(force_namespace=True))}
    assert names == {f"mnesis__{n}" for n in MNESIS_TOOL_NAMES}


def test_namespacing_does_not_mutate_original_tool():
    # Renaming a copy must leave the source's own tool object untouched.
    src = FakeMnesisTools()
    run(ToolRegistry([src, _OtherSource()]).get_tools())  # triggers renaming
    again = {t.name for t in run(ToolRegistry([src]).get_tools())}
    assert "mnesis_query" in again  # bare name, not the namespaced one


def test_empty_registry():
    assert run(ToolRegistry().get_tools()) == []


# ── connection config + real MCP path (mocked transport) ──────────────────


def test_mnesis_connection_carries_url_and_bearer(monkeypatch):
    monkeypatch.setattr(agents_config, "MNESIS_MCP_URL", "http://mnesis:8080/mcp")
    monkeypatch.setattr(agents_config, "MNESIS_MCP_TOKEN", "secret-token")
    conn = mnesis_connection()
    assert conn["transport"] == "streamable_http"
    assert conn["url"] == "http://mnesis:8080/mcp"
    assert conn["headers"]["Authorization"] == "Bearer secret-token"


def test_mnesis_connection_omits_header_without_token(monkeypatch):
    monkeypatch.setattr(agents_config, "MNESIS_MCP_TOKEN", None)
    assert "headers" not in mnesis_connection()


def test_mcp_source_builds_client_and_loads_tools(monkeypatch):
    # Mock MultiServerMCPClient: record the connections it's built with and return
    # canned tools from get_tools — exercises the real MCPToolSource path with the
    # transport stubbed (no network).
    captured = {}

    @tool
    def mnesis_query(query: str) -> str:
        """canned"""
        return "ok"

    class FakeClient:
        def __init__(self, connections, **kwargs):
            captured["connections"] = connections

        async def get_tools(self, *, server_name=None):
            return [mnesis_query]

    import langchain_mcp_adapters.client as client_mod
    monkeypatch.setattr(client_mod, "MultiServerMCPClient", FakeClient)

    src = MCPToolSource({"mnesis": {"transport": "streamable_http", "url": "http://x/mcp",
                                    "headers": {"Authorization": "Bearer t"}}})
    tools = run(src.load_tools())
    assert [t.name for t in tools] == ["mnesis_query"]
    assert captured["connections"]["mnesis"]["url"] == "http://x/mcp"
    assert captured["connections"]["mnesis"]["headers"]["Authorization"] == "Bearer t"


def test_mcp_source_connection_failure_raises_clear_error(monkeypatch):
    class FakeClient:
        def __init__(self, connections, **kwargs):
            pass

        async def get_tools(self, *, server_name=None):
            raise ConnectionRefusedError("connection refused")

    import langchain_mcp_adapters.client as client_mod
    monkeypatch.setattr(client_mod, "MultiServerMCPClient", FakeClient)

    src = MCPToolSource({"mnesis": {"transport": "streamable_http", "url": "http://down/mcp"}})
    with pytest.raises(MnesisConnectionError) as ei:
        run(src.load_tools())
    msg = str(ei.value)
    assert "http://down/mcp" in msg and "MNESIS_MCP_TOKEN" in msg  # actionable
