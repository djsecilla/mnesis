"""Tests for the MCP HTTP transport: /health + bearer-token auth (stub mode).

Starts the streamable-HTTP app under uvicorn on an ephemeral port in a daemon
thread, then drives it with httpx (health) and a real MCP client (tool call).
"""

from __future__ import annotations

import asyncio
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from mnesis import config, ingest, mcp_server, tenancy

TOKEN = "s3cret-token"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def http_server():
    """A running HTTP MCP server (module-scoped) with a token and a seeded page."""
    tmp = Path(tempfile.mkdtemp(prefix="mnesis-http-"))
    saved = {
        k: getattr(config, k)
        for k in (
            "DATA_ROOT", "MNESIS_LLM_STUB", "MNESIS_MCP_HOST", "MNESIS_MCP_PORT", "MNESIS_MCP_TOKEN",
        )
    }
    config.DATA_ROOT = tmp / "data"
    config.MNESIS_LLM_STUB = True
    config.MNESIS_MCP_TOKEN = TOKEN

    # Provision + bind the default tenant for fixture-time seeding; the running
    # server rebinds it per request via the tenant-binding middleware.
    _ctx = tenancy.open_tenant(config.DEFAULT_TENANT_ID)
    _token = tenancy.bind(_ctx)

    # Seed a page so /health stats and a tool call have something to report.
    ingest.ingest_source("Project Atlas uses Redis for caching.", "atlas")
    mcp_server.mnesis_rebuild()

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(mcp_server.build_http_app(), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    for _ in range(100):  # wait for readiness
        try:
            if httpx.get(f"{base}/health", timeout=0.5).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    else:
        raise RuntimeError("HTTP MCP server did not become ready")

    yield base

    server.should_exit = True
    thread.join(timeout=5)
    tenancy.unbind(_token)
    for k, v in saved.items():
        setattr(config, k, v)
    shutil.rmtree(tmp, ignore_errors=True)


async def _call_tool(url: str, name: str, args: dict, token: str | None):
    headers = {"Authorization": f"Bearer {token}"} if token else None
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool(name, args)


def test_health_returns_200_with_stats(http_server):
    r = httpx.get(f"{http_server}/health", timeout=5)
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["pages"] >= 1
    assert data["index_present"] is True
    assert data["graph_present"] is True


def test_health_is_unauthenticated(http_server):
    # No token header, yet /health is reachable (safe for probes).
    assert httpx.get(f"{http_server}/health", timeout=5).status_code == 200


def test_tool_call_rejected_without_token(http_server):
    # The MCP endpoint requires the bearer token; a bare POST is 401.
    r = httpx.post(
        f"{http_server}/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        headers={"accept": "application/json, text/event-stream", "content-type": "application/json"},
        timeout=5,
    )
    assert r.status_code == 401


def test_tool_call_succeeds_with_token(http_server):
    result = asyncio.run(_call_tool(f"{http_server}/mcp", "mnesis_list", {}, TOKEN))
    assert not result.isError
    text = "".join(getattr(c, "text", "") for c in result.content)
    assert "project-atlas-uses-redis-for-caching" in text


def test_mcp_client_rejected_without_token(http_server):
    # A full client handshake without the token must fail.
    with pytest.raises(Exception):
        asyncio.run(_call_tool(f"{http_server}/mcp", "mnesis_list", {}, None))
