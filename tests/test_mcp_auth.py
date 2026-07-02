"""IAM7 — MCP authentication & scoped tool authorization (agents & clients).

The MCP surface authenticates every call with a per-tenant, per-agent **agent key**
(IAM3) resolving to an AuthenticatedPrincipal + tenant + scopes (IAM1), and every
``mnesis_*`` tool enforces the credential's scopes through the PDP (IAM4). The single
global MCP token is retired.

Two layers are exercised:
  - **Scope enforcement** — in-process, binding the exact ``(tenant, principal)`` an
    agent key resolves to (`tokens.resolve_bearer` — the same call the server middleware
    makes), so a tool run under it sees precisely what an MCP client would over the wire.
  - **Transport** — a real HTTP server: an unauthenticated call is refused, a valid key
    works, a revoked key stops immediately, the global-token path is gone, and a
    tenant-A key reaches only tenant A.
"""

from __future__ import annotations

import contextlib
import socket
import tempfile
import threading
import time
import types
from pathlib import Path

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from mnesis import auth, config, mcp_server, search, store, tenancy, tokens
from mnesis.store import Page


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@contextlib.contextmanager
def _as(raw_key: str):
    """Bind the (tenant, principal) an agent key resolves to — exactly the server's
    per-request binding — for an in-process tool call."""
    ctx, principal = tokens.resolve_bearer(raw_key)
    with tenancy.use(ctx):
        tok = auth.bind_principal(principal)
        try:
            yield
        finally:
            auth.unbind_principal(tok)


@pytest.fixture()
def keys(tmp_path, monkeypatch):
    """A data root with a seeded 'default' tenant and least-privilege agent keys of
    each kind (read / write / maintain), via the documented per-agent mapping."""
    monkeypatch.setattr(config, "DATA_ROOT", tmp_path / "data", raising=False)
    monkeypatch.setattr(config, "MNESIS_LLM_STUB", True, raising=False)
    ctx = tenancy.create_tenant(config.DEFAULT_TENANT_ID, data_root=config.DATA_ROOT)
    with tenancy.use(ctx):
        store.write_page(Page(id="atlas", title="Atlas uses Redis", body="Atlas uses Redis."))
        search.rebuild()
    svc = tokens.TokenService()
    read_key, _ = tokens.issue_agent_key_for("action", config.DEFAULT_TENANT_ID, "reader", service=svc)
    write_key, wrec = tokens.issue_agent_key_for("writing", config.DEFAULT_TENANT_ID, "writer", service=svc)
    maint_key, _ = tokens.issue_agent_key_for("maintenance", config.DEFAULT_TENANT_ID, "curator", service=svc)
    return types.SimpleNamespace(svc=svc, read=read_key, write=write_key, maint=maint_key,
                                 write_id=wrec.id)


# ── the tool → scope mapping is explicit and complete ──────────────────────


def test_every_tool_has_an_explicit_scope():
    # Every registered mnesis_* tool is in the explicit scope map (no unguarded tool).
    tool_names = {n for n in dir(mcp_server) if n.startswith("mnesis_") and callable(getattr(mcp_server, n))}
    assert tool_names == set(mcp_server._TOOL_SCOPES)


def test_agent_keys_carry_least_privilege_scopes(keys):
    assert tokens.resolve_bearer(keys.read)[1].scopes == frozenset({"read"})
    assert tokens.resolve_bearer(keys.write)[1].scopes == frozenset({"write"})
    assert tokens.resolve_bearer(keys.maint)[1].scopes == frozenset({"read", "maintain"})


# ── scope enforcement per tool (role ∩ scope) ──────────────────────────────


def test_read_scoped_key_can_query_but_not_ingest(keys):
    with _as(keys.read):
        assert "atlas" in mcp_server.mnesis_list()          # read allowed
        assert "Atlas" in mcp_server.mnesis_query("redis") or "atlas" in mcp_server.mnesis_query("redis")
        with pytest.raises(Exception):                       # write denied by scope
            mcp_server.mnesis_ingest("New Atlas fact.", "src-read")


def test_writing_key_can_ingest_but_not_read_or_maintain(keys):
    # A writing-agent key (write scope) can ingest, but cannot query/read or run
    # maintenance ("send"/other lanes stay out of scope; egress is separate anyway).
    with _as(keys.write):
        out = mcp_server.mnesis_ingest("Redis is a cache used by Atlas.", "src-write")
        assert "ingested page" in out
        with pytest.raises(Exception):
            mcp_server.mnesis_query("redis")                 # read not in scope
        with pytest.raises(Exception):
            mcp_server.mnesis_decay()                        # maintain not in scope


def test_maintenance_key_can_maintain_and_read_but_not_write(keys):
    with _as(keys.maint):
        assert "decay:" in mcp_server.mnesis_decay()         # maintain allowed
        mcp_server.mnesis_list()                             # read allowed
        with pytest.raises(Exception):
            mcp_server.mnesis_ingest("x", "src-maint")       # write denied


# ── the live HTTP surface ──────────────────────────────────────────────────


@pytest.fixture()
def live(keys, monkeypatch):
    """A running HTTP server exercising the **IAM7 /mcp auth middleware** on a standalone
    app (a stub /mcp handler behind ``_PrincipalBindingMiddleware``), so we test auth
    without rebuilding the once-per-process FastMCP streamable app (see test_mcp_http)."""
    monkeypatch.setattr(config, "MNESIS_MCP_TOKEN", "", raising=False)
    # A second tenant with its own key, to prove cross-tenant isolation.
    beta = tenancy.create_tenant("beta", data_root=config.DATA_ROOT)
    with tenancy.use(beta):
        store.write_page(Page(id="beta-only", title="Beta secret", body="beta only."))
        search.rebuild()
    beta_key, _ = tokens.issue_agent_key_for("maintenance", "beta", "beta-agent", service=keys.svc)

    async def _mcp(_request):  # reached only after the middleware authenticates the key
        return JSONResponse({"ok": True, "tenant": tenancy.current().tenant_id})

    async def _health(_request):
        return JSONResponse(mcp_server._health_payload())

    app = Starlette(routes=[Route("/mcp", _mcp, methods=["POST"]),
                            Route("/health", _health, methods=["GET"])])
    app.add_middleware(mcp_server._PrincipalBindingMiddleware)
    app.add_middleware(mcp_server._HealthTenantMiddleware)

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(f"{base}/health", timeout=0.5).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    else:
        raise RuntimeError("server did not become ready")
    yield types.SimpleNamespace(base=base, read=keys.read, beta_key=beta_key)
    server.should_exit = True
    thread.join(timeout=5)


def _init(base: str, key: str | None):
    """A minimal MCP initialize POST; returns the HTTP status code."""
    headers = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
    if key:
        headers["authorization"] = f"Bearer {key}"
    r = httpx.post(f"{base}/mcp",
                   json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                    "clientInfo": {"name": "t", "version": "0"}}},
                   headers=headers, timeout=5)
    return r.status_code


def test_unauthenticated_mcp_call_is_refused(live):
    assert _init(live.base, None) == 401                     # no credential → 401
    assert _init(live.base, "not-a-real-key") == 401         # bogus credential → 401


def test_global_token_path_is_gone(live, monkeypatch):
    # Even with a configured MNESIS_MCP_TOKEN, it does not authenticate /mcp anymore.
    monkeypatch.setattr(config, "MNESIS_MCP_TOKEN", "the-old-global-token", raising=False)
    assert _init(live.base, "the-old-global-token") == 401


def test_valid_key_authenticates_and_revocation_is_immediate(live, keys):
    assert _init(live.base, live.read) == 200                # valid agent key works
    keys.svc.revoke_token(live.read)                         # revoke it…
    assert _init(live.base, live.read) == 401                # …denied immediately


def test_tenant_a_key_reaches_only_tenant_a(live):
    # The beta key authenticates (200) but is bound to beta — never default's data.
    assert _init(live.base, live.beta_key) == 200
    with _as(live.beta_key):
        assert tenancy.current().tenant_id == "beta"
        assert "beta-only" in mcp_server.mnesis_list()
        assert "atlas" not in mcp_server.mnesis_list()       # default's page is unreachable
