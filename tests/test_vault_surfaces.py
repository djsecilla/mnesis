"""V5 — surface & agent vault-scoping (CLAUDE.md §16 Vaults).

The active vault is threaded through every surface (Web / CLI / MCP) and every agent as a
client SELECTION that is always RE-AUTHORIZED server-side against the principal's grants —
one choke point per surface, fail closed. With two granted vaults, each surface returns
only the active vault's data; selecting an ungranted vault via any surface is denied; an
MCP/agent credential reaches only its vault; a maintenance op curates only its vault; and
switching the active vault never leaks the previous vault's data.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from mnesis import (
    auth,
    authz,
    config,
    graph,
    identity,
    mcp_server,
    providers,
    search,
    store,
    tenancy,
    webapi,
    webauth,
)
from mnesis.store import Page
from mnesis_agents import tenancy as agent_tenancy
from mnesis_agents.knowledge import mnesis_connection

ALICE_PW = "alice-strong-passphrase-1"
T = config.DEFAULT_TENANT_ID


def _seed(ctx, pages):
    with tenancy.use(ctx):
        for pid, title in pages:
            store.write_page(Page(id=pid, title=title, body=f"{title}. It uses Redis.",
                                  tags=["library:redis"],
                                  relations=[{"s": "library:redis", "p": "related_to", "o": "concept:cache"}]))
        search.rebuild()
        graph.rebuild_graph()


@pytest.fixture()
def env(monkeypatch):
    """The default tenant with member ``alice`` granted vaults ``alpha`` + ``beta`` (each
    seeded with distinct data) and an ungranted ``gamma`` (owned by bob). Yields the
    in-process /api app, alice's bearer token, and the data root."""
    tmp = Path(tempfile.mkdtemp(prefix="mnesis-vault-surfaces-"))
    monkeypatch.setattr(config, "DATA_ROOT", tmp / "data")
    monkeypatch.setattr(config, "MNESIS_LLM_STUB", True)
    monkeypatch.setattr(config, "MNESIS_WEB_COOKIE_SECURE", False)  # TestClient speaks http

    root = config.DATA_ROOT
    tenancy.open_tenant(T)  # provision the tenant + default vault
    providers.LocalPasswordProvider().register(T, "alice", "member", ALICE_PW)
    tok, _ = auth.CredentialStore().issue(T, "alice", "member")  # bearer for MCP/agent paths

    A = tenancy.create_vault(T, "alpha", owner_principal="alice", data_root=root)
    B = tenancy.create_vault(T, "beta", owner_principal="alice", data_root=root)
    tenancy.create_vault(T, "gamma", owner_principal="bob", data_root=root)  # alice NOT granted
    _seed(A, [("a1", "Alpha primary note"), ("a2", "Alpha secondary note")])
    _seed(B, [("b1", "Beta only note")])

    application = Starlette()
    webapi.mount_api(application)
    webauth.install(application)
    yield application, tok, root
    shutil.rmtree(tmp, ignore_errors=True)


def _login(client: TestClient) -> None:
    r = client.post("/api/auth/login", json={"username": "alice", "password": ALICE_PW})
    assert r.status_code == 200, r.text


# ── Web surface: header selection, re-authorized per request ────────────────


def test_web_lists_only_accessible_vaults(env):
    application, tok, root = env
    client = TestClient(application)
    _login(client)
    body = client.get("/api/vaults").json()
    assert {v["vault_id"] for v in body["vaults"]} == {"default", "alpha", "beta"}  # gamma NOT listed
    assert body["active_vault"] == "default"


def test_web_scopes_to_the_active_vault(env):
    application, tok, root = env
    client = TestClient(application)
    _login(client)
    a = {p["id"] for p in client.get("/api/pages", headers={"X-Mnesis-Vault": "alpha"}).json()["pages"]}
    b = {p["id"] for p in client.get("/api/pages", headers={"X-Mnesis-Vault": "beta"}).json()["pages"]}
    assert a == {"a1", "a2"} and b == {"b1"}
    # Search is scoped too — alpha never surfaces beta's page and vice-versa.
    a_hits = {h["id"] for h in client.get("/api/search?q=redis", headers={"X-Mnesis-Vault": "alpha"}).json()["hits"]}
    assert a_hits == {"a1", "a2"}
    b_hits = {h["id"] for h in client.get("/api/search?q=redis", headers={"X-Mnesis-Vault": "beta"}).json()["hits"]}
    assert b_hits == {"b1"}


def test_web_denies_an_ungranted_vault_selection(env):
    application, tok, root = env
    client = TestClient(application)
    _login(client)
    r = client.get("/api/pages", headers={"X-Mnesis-Vault": "gamma"})
    assert r.status_code == 403 and r.json()["error"] == "vault_forbidden"
    # An unknown vault likewise fails closed (no default fallback).
    assert client.get("/api/pages", headers={"X-Mnesis-Vault": "nope"}).status_code == 403


def test_web_switching_vaults_never_leaks(env):
    application, tok, root = env
    client = TestClient(application)
    _login(client)
    for _ in range(2):  # alpha → beta → alpha, repeatedly, same session
        a = {p["id"] for p in client.get("/api/pages", headers={"X-Mnesis-Vault": "alpha"}).json()["pages"]}
        b = {p["id"] for p in client.get("/api/pages", headers={"X-Mnesis-Vault": "beta"}).json()["pages"]}
        assert a == {"a1", "a2"} and b == {"b1"}
        assert not (a & b)  # never mixed


# ── MCP surface: the credential + vault header, re-authorized ───────────────
# `authz.authenticated_vault(token, vault)` is exactly what the /mcp choke point does per
# request: resolve the bearer, authorize the vault selection, bind (vault, principal). A
# tool run inside it sees precisely what an MCP client would over the wire.


def test_mcp_tools_scope_to_the_selected_vault(env):
    application, tok, root = env
    with authz.authenticated_vault(tok, "alpha"):
        a_list = mcp_server.mnesis_list()
        a_q = mcp_server.mnesis_query("redis")
    with authz.authenticated_vault(tok, "beta"):
        b_list = mcp_server.mnesis_list()
    assert "Alpha primary note" in a_list and "Beta" not in a_list
    assert "Alpha" in a_q and "Beta" not in a_q
    assert "Beta only note" in b_list and "Alpha" not in b_list


def test_mcp_denies_an_ungranted_or_unknown_vault(env):
    application, tok, root = env
    for bad in ("gamma", "nope"):
        with pytest.raises(identity.Deny):
            with authz.authenticated_vault(tok, bad):
                pass


def test_mcp_cross_vault_reference_is_not_found(env):
    application, tok, root = env
    # A page id that exists only in beta is unreachable while bound to alpha.
    with authz.authenticated_vault(tok, "beta"):
        assert "Beta only note" in mcp_server.mnesis_get("b1")  # present in its own vault
    with authz.authenticated_vault(tok, "alpha"):
        assert mcp_server.mnesis_get("b1").startswith("no such page")  # absent cross-vault


# ── CLI surface: --vault selection, re-authorized ───────────────────────────


def test_cli_resolves_and_authorizes_the_vault(env, monkeypatch):
    from mnesis import cli
    import types

    application, tok, root = env
    monkeypatch.setenv("MNESIS_TOKEN", tok)
    # A granted vault resolves + scopes.
    args = types.SimpleNamespace(token=None, tenant=T, vault="alpha")
    ctx, principal = cli._resolve_data_context(args)
    assert ctx.vault_id == "alpha" and principal.principal_id == "alice"
    with tenancy.use(ctx):
        assert {p.id for p in store.list_pages()} == {"a1", "a2"}
    # An ungranted vault is refused (fail closed).
    args_bad = types.SimpleNamespace(token=None, tenant=T, vault="gamma")
    with pytest.raises(cli._CliDenied):
        cli._resolve_data_context(args_bad)


# ── Maintenance op curates only its vault ───────────────────────────────────


def test_maintenance_op_curates_only_its_vault(env):
    application, tok, root = env
    # A maintenance diagnostic (what the dream cycle's quality-sweep runs) reflects only
    # the bound vault — alpha has 2 pages, beta 1.
    with authz.authenticated_vault(tok, "alpha"):
        h_a = mcp_server.mnesis_health_report()
    with authz.authenticated_vault(tok, "beta"):
        h_b = mcp_server.mnesis_health_report()
    assert "2" in h_a and h_a != h_b  # distinct, per-vault health
    # A maintenance write (decay/lifecycle) under alpha never touches beta's cache.
    b_wiki_mtime = tenancy.context_for(T, "beta", data_root=root).cache_path("wiki.db").stat().st_mtime_ns
    with authz.authenticated_vault(tok, "alpha"):
        mcp_server.mnesis_decay()
        mcp_server.mnesis_rebuild()
    assert tenancy.context_for(T, "beta", data_root=root).cache_path("wiki.db").stat().st_mtime_ns == b_wiki_mtime


# ── Agent: each agent is confined to one (tenant, vault) ────────────────────


def test_agent_scope_is_per_vault_and_presents_its_vault(env, tmp_path):
    application, tok, root = env
    sa = agent_tenancy.resolve_scope(T, tok, vault_id="alpha", state_base=tmp_path / "agents")
    sb = agent_tenancy.resolve_scope(T, tok, vault_id="beta", state_base=tmp_path / "agents")
    # Per-vault governance state — no directory is shared across vaults.
    assert sa.vault_id == "alpha" and sb.vault_id == "beta"
    assert sa.state_root != sb.state_root
    assert "alpha" in sa.state_root.parts and "beta" in sb.state_root.parts
    assert sa.audit_dir != sb.audit_dir and sa.egress_ledger != sb.egress_ledger
    # The agent presents its vault to Mnesis over MCP (re-authorized server-side).
    headers = mnesis_connection(token=tok, vault="alpha")["headers"]
    assert headers[config.VAULT_SELECTION_HEADER] == "alpha"
    assert headers["Authorization"] == f"Bearer {tok}"
