"""T5 — tenant enforcement across MCP, the Web UI gateway (REST+SSE), and the CLI.

Two tenants (alpha, beta) with overlapping topics, each reached only through a
credential. The single per-surface choke point binds the credential's
(TenantContext, Principal); no surface accepts a client-supplied tenant. We assert
across a REAL server: no MCP tool, REST endpoint, or SSE stream returns B's data to
A (or vice-versa); a forged/extra tenant id is ignored in favour of the credential's
tenant; an unauthenticated request is denied; and the CLI refuses tenant ops without
a resolved authenticated context.
"""

from __future__ import annotations

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

from mnesis import auth, config, graph, mcp_server, search, store, tenancy, webapi
from mnesis.store import Page


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _seed(team: str) -> None:
    slug = team.lower()
    store.write_page(Page(
        id="redis-cache", title=f"{team} team uses Redis for caching",
        body=f"The {team} team uses Redis as its cache.",
        tags=[f"project:{slug}", "library:redis"],
        relations=[{"s": f"project:{slug}", "p": "uses", "o": "library:redis"}],
    ))
    search.rebuild()
    graph.rebuild_graph()


@pytest.fixture(scope="module")
def live():
    """A running auth-enabled server with two seeded tenants + a credential each."""
    tmp = Path(tempfile.mkdtemp(prefix="mnesis-surfaces-"))
    saved = {k: getattr(config, k) for k in (
        "DATA_ROOT", "MNESIS_AUTH_ENABLED", "MNESIS_MCP_TOKEN", "MNESIS_LLM_STUB",
        "MNESIS_MCP_HOST", "MNESIS_MCP_PORT",
    )}
    config.DATA_ROOT = tmp / "data"
    config.MNESIS_AUTH_ENABLED = True        # credential auth at the boundary
    config.MNESIS_MCP_TOKEN = ""             # legacy single-token path OFF
    config.MNESIS_LLM_STUB = True

    creds = auth.CredentialStore()
    a = tenancy.create_tenant("alpha", data_root=config.DATA_ROOT)
    b = tenancy.create_tenant("beta", data_root=config.DATA_ROOT)
    with tenancy.use(a):
        _seed("Alpha")
    with tenancy.use(b):
        _seed("Beta")
    tok_a, _ = creds.issue("alpha", "ann", "member")
    tok_b, _ = creds.issue("beta", "bob", "member")

    # A REAL HTTP server for the REST + SSE gateway. We build a STANDALONE app
    # (the same /api routes via mount_api + the credential-binding middleware), not
    # the full FastMCP app — `mcp.streamable_http_app()` can only be built once per
    # process (test_mcp_http already builds it). The MCP *tool* path is exercised
    # in-process through the identical binding (`auth.authenticated`).
    async def _health(_req):
        return JSONResponse(mcp_server._health_payload())

    app = Starlette(routes=[Route("/health", _health, methods=["GET"])])
    webapi.mount_api(app)
    app.add_middleware(mcp_server._PrincipalBindingMiddleware)

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

    yield types.SimpleNamespace(base=base, tok_a=tok_a, tok_b=tok_b)

    server.should_exit = True
    thread.join(timeout=5)
    for k, v in saved.items():
        setattr(config, k, v)


# ── helpers ─────────────────────────────────────────────────────────────────


def _h(token: str | None, **extra) -> dict:
    headers = dict(extra)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


# ── MCP tools (the same functions /mcp serves), run under the real binding ──
# `auth.authenticated(token)` is exactly what the server's _PrincipalBindingMiddleware
# does per request: resolve the credential → bind (tenant, principal). So a tool run
# inside it sees precisely what an MCP client would over the wire.


def test_mcp_tools_return_only_the_credentials_tenant(live):
    with auth.authenticated(live.tok_a):
        a_list, a_q, a_get = (
            mcp_server.mnesis_list(),
            mcp_server.mnesis_query("redis"),
            mcp_server.mnesis_get("redis-cache"),
        )
    with auth.authenticated(live.tok_b):
        b_list = mcp_server.mnesis_list()
        b_get = mcp_server.mnesis_get("redis-cache")
    assert "Alpha team uses Redis" in a_list and "Beta" not in a_list
    assert "Beta team uses Redis" in b_list and "Alpha" not in b_list
    assert "Alpha team uses Redis" in a_q and "Beta" not in a_q
    # The same page id holds different content per tenant — never the other's.
    assert "Alpha" in a_get and "Beta" not in a_get
    assert "Beta" in b_get and "Alpha" not in b_get


def test_mcp_tool_path_denies_an_invalid_or_absent_credential(live):
    for bad in (None, "", "not-a-real-token"):
        with pytest.raises(auth.InvalidCredential):
            with auth.authenticated(bad):
                pass


# ── REST gateway ────────────────────────────────────────────────────────────


def _get(live, path, token, **extra):
    return httpx.get(f"{live.base}{path}", headers=_h(token, **extra), timeout=5)


def test_rest_pages_and_detail_are_tenant_scoped(live):
    a = _get(live, "/api/pages", live.tok_a).json()["pages"]
    b = _get(live, "/api/pages", live.tok_b).json()["pages"]
    assert [p["title"] for p in a] == ["Alpha team uses Redis for caching"]
    assert [p["title"] for p in b] == ["Beta team uses Redis for caching"]
    # Same id, tenant-specific content.
    assert _get(live, "/api/pages/redis-cache", live.tok_a).json()["frontmatter"]["title"].startswith("Alpha")
    assert _get(live, "/api/pages/redis-cache", live.tok_b).json()["frontmatter"]["title"].startswith("Beta")


def test_rest_search_graph_entity_are_tenant_scoped(live):
    a_hits = _get(live, "/api/search?q=redis", live.tok_a).json()["hits"]
    assert [h["title"] for h in a_hits] == ["Alpha team uses Redis for caching"]

    a_nodes = {n["ref"] for n in _get(live, "/api/graph", live.tok_a).json()["nodes"]}
    assert "project:alpha" in a_nodes and "project:beta" not in a_nodes

    # Alpha cannot resolve Beta's unique entity; Beta can.
    assert _get(live, "/api/entity/project:beta", live.tok_a).status_code == 404
    assert _get(live, "/api/entity/project:beta", live.tok_b).status_code == 200
    assert _get(live, "/api/entity/project:alpha", live.tok_a).status_code == 200


def test_rest_sources_are_tenant_scoped(live):
    a_src = _get(live, "/api/sources", live.tok_a).json()["sources"]
    titles = [pg["title"] for s in a_src for pg in s["pages"]]
    assert all("Alpha" in t for t in titles) and not any("Beta" in t for t in titles)


def test_a_forged_or_extra_tenant_id_is_ignored(live):
    """A request carrying beta's id (header + query) under alpha's credential still
    resolves to alpha — the client-supplied tenant is never trusted."""
    r = _get(live, "/api/pages?tenant_id=beta", live.tok_a, **{"X-Tenant-Id": "beta"})
    assert [p["title"] for p in r.json()["pages"]] == ["Alpha team uses Redis for caching"]
    page = _get(live, "/api/pages/redis-cache?tenant=beta", live.tok_a,
                **{"X-Tenant-Id": "beta"}).json()
    assert page["frontmatter"]["title"].startswith("Alpha")


def test_rest_denies_unauthenticated_requests(live):
    for path in ("/api/pages", "/api/pages/redis-cache", "/api/search?q=redis",
                 "/api/graph", "/api/sources", "/api/entity/project:alpha"):
        assert _get(live, path, None).status_code == 401
    # /health stays open (liveness, tenant-agnostic).
    assert _get(live, "/health", None).status_code == 200


# ── SSE (chat) ──────────────────────────────────────────────────────────────


def test_sse_chat_stream_is_tenant_scoped(live):
    chunks: list[str] = []
    with httpx.stream(
        "POST", f"{live.base}/api/chat", json={"message": "redis caching"},
        headers=_h(live.tok_a), timeout=10,
    ) as r:
        assert r.status_code == 200
        for text in r.iter_text():
            chunks.append(text)
    body = "".join(chunks)
    assert "Alpha" in body and "Beta" not in body          # only alpha's page is grounded
    # And unauthenticated SSE is denied.
    with httpx.stream("POST", f"{live.base}/api/chat", json={"message": "redis"},
                      headers={}, timeout=5) as r:
        assert r.status_code == 401


# ── CLI ─────────────────────────────────────────────────────────────────────


@pytest.fixture()
def cli_tenant(tmp_path, monkeypatch):
    """A fresh data root with one tenant + a credential, for in-process CLI tests."""
    from mnesis import cli  # noqa: F401  (ensures import is wired)

    root = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_ROOT", root, raising=False)
    monkeypatch.setattr(config, "MNESIS_LLM_STUB", True, raising=False)
    ctx = tenancy.create_tenant("acme", data_root=root)
    with tenancy.use(ctx):
        _seed("Acme")
    token, _ = auth.CredentialStore().issue("acme", "alice", "member")
    return token


def test_cli_refuses_tenant_ops_without_a_credential_when_auth_enabled(cli_tenant, monkeypatch, capsys):
    from mnesis import cli

    monkeypatch.setattr(config, "MNESIS_AUTH_ENABLED", True, raising=False)
    monkeypatch.delenv("MNESIS_CREDENTIAL", raising=False)
    rc = cli.main(["query", "redis"])
    out = capsys.readouterr().out
    assert rc == 2 and "no credential" in out.lower()


def test_cli_resolves_tenant_from_credential_ignoring_tenant_flag(cli_tenant, monkeypatch, capsys):
    from mnesis import cli

    monkeypatch.setattr(config, "MNESIS_AUTH_ENABLED", True, raising=False)
    monkeypatch.setenv("MNESIS_CREDENTIAL", cli_tenant)
    # A forged --tenant must be ignored in favour of the credential's tenant (acme).
    rc = cli.main(["--tenant", "someone-else", "query", "redis"])
    out = capsys.readouterr().out
    assert rc == 0 and "Acme team uses Redis" in out


def test_cli_invalid_credential_is_denied(cli_tenant, monkeypatch, capsys):
    from mnesis import cli

    monkeypatch.setattr(config, "MNESIS_AUTH_ENABLED", True, raising=False)
    monkeypatch.setenv("MNESIS_CREDENTIAL", "not-a-real-token")
    rc = cli.main(["query", "redis"])
    assert rc == 2 and "rejected" in capsys.readouterr().out.lower()
