"""T4 — authorization & within-tenant visibility (CLAUDE.md §16).

One tenant, two principals. A **private** page owned by P1 is invisible to P2 in
search/get/graph/traverse/impact — by **any** query path — while a **shared** page
is visible to both. Writes respect role (readonly is denied), and ingest/file-back
stamp owner + visibility. Cross-tenant is already impossible (T1/T2); this narrows
*within* a tenant.
"""

from __future__ import annotations

import contextlib

import pytest

from mnesis import auth, authz, config, graph, ingest, mcp_server, search, store, tenancy
from mnesis.store import Page

P1 = auth.Principal("p1", "acme", "member")
P2 = auth.Principal("p2", "acme", "member")
READER = auth.Principal("rdr", "acme", "readonly")
ADMIN = auth.Principal("boss", "acme", "admin")


@contextlib.contextmanager
def as_principal(ctx, principal):
    """Bind the tenant + a principal for the block (the boundary, in a test)."""
    with tenancy.use(ctx):
        token = auth.bind_principal(principal)
        try:
            yield
        finally:
            auth.unbind_principal(token)


@pytest.fixture()
def acme(tmp_path, monkeypatch):
    """A tenant with a PRIVATE page (owned by p1) and a SHARED page, caches built."""
    root = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_ROOT", root, raising=False)
    monkeypatch.setattr(config, "MNESIS_LLM_STUB", True, raising=False)
    ctx = tenancy.create_tenant("acme", data_root=root)
    with tenancy.use(ctx):
        # Private to p1: unique entity library:vault, edge falcon -> uses -> vault.
        store.write_page(Page(
            id="falcon-vault", title="Project Falcon uses Vault for secrets",
            body="Project Falcon stores its secrets in Vault.",
            owner_principal="p1", visibility="private",
            tags=["project:falcon", "library:vault"],
            relations=[{"s": "project:falcon", "p": "uses", "o": "library:vault"}],
        ))
        # Shared: entity library:redis, edge atlas -> uses -> redis.
        store.write_page(Page(
            id="atlas-redis", title="Project Atlas uses Redis for caching",
            body="Project Atlas caches hot data in Redis.",
            owner_principal="p1", visibility="shared",
            tags=["project:atlas", "library:redis"],
            relations=[{"s": "project:atlas", "p": "uses", "o": "library:redis"}],
        ))
        search.rebuild()
        graph.rebuild_graph()
    return ctx


# ── search: private invisible to non-owner, shared visible to both ──────────


def test_search_hides_private_pages_from_non_owners(acme):
    with as_principal(acme, P2):
        assert {h.id for h in search.search("vault")} == set()        # private → hidden
        assert {h.id for h in search.search("falcon")} == set()
        assert {h.id for h in search.search("redis")} == {"atlas-redis"}  # shared → visible
    with as_principal(acme, P1):
        assert {h.id for h in search.search("vault")} == {"falcon-vault"}  # owner sees it
        assert {h.id for h in search.search("redis")} == {"atlas-redis"}
    with as_principal(acme, ADMIN):
        assert {h.id for h in search.search("vault")} == {"falcon-vault"}  # admin sees all


# ── get: private 404s for non-owner, readable by owner/admin ────────────────


def test_get_hides_a_private_page_from_non_owner(acme):
    with as_principal(acme, P2):
        assert "no such page" in mcp_server.mnesis_get("falcon-vault").lower()
        assert "Project Atlas" in mcp_server.mnesis_get("atlas-redis")  # shared visible
    with as_principal(acme, P1):
        assert "Project Falcon" in mcp_server.mnesis_get("falcon-vault")
    with as_principal(acme, ADMIN):
        assert "Project Falcon" in mcp_server.mnesis_get("falcon-vault")


# ── graph: entity/neighbors/traverse/impact all hide private-only nodes ─────


def test_graph_hides_private_only_entities_and_edges(acme):
    with as_principal(acme, P2):
        assert graph.entity("library:vault") is None          # only a private page backs it
        assert graph.entity("library:redis") is not None      # shared
        assert graph.neighbors("library:vault", direction="in") == []
        assert graph.impact("library:vault") == []
        assert graph.traverse("project:falcon") == []         # nothing visible to reach
    with as_principal(acme, P1):
        assert graph.entity("library:vault") is not None
        assert {n["ref"] for n in graph.neighbors("library:vault", direction="in")} == {"project:falcon"}
        assert {a["ref"] for a in graph.impact("library:vault")} == {"project:falcon"}


def test_graph_query_never_folds_in_a_private_page(acme):
    with as_principal(acme, P2):
        assert {h.id for h in graph.graph_query("vault")} == set()
        assert {h.id for h in graph.graph_query("falcon")} == set()
    with as_principal(acme, P1):
        assert "falcon-vault" in {h.id for h in graph.graph_query("vault")}


def test_p2_cannot_reach_p1s_private_page_by_any_query_path(acme):
    """The single most important guarantee: no query surface leaks the private page."""
    with as_principal(acme, P2):
        for got in (
            {h.id for h in search.search("falcon")},
            {h.id for h in search.search("vault secrets")},
            {h.id for h in graph.graph_query("falcon vault")},
            {a["ref"] for a in graph.impact("library:vault")},
            {n["ref"] for n in graph.neighbors("library:vault", direction="in")},
            {t["ref"] for t in graph.traverse("project:falcon", depth=3)},
        ):
            assert "falcon-vault" not in got and "project:falcon" not in got
        assert graph.entity("project:falcon") is None
        assert "no such page" in mcp_server.mnesis_get("falcon-vault").lower()


# ── authorization: roles & ownership ────────────────────────────────────────


def test_readonly_principal_cannot_write(acme):
    with as_principal(acme, READER):
        with pytest.raises(authz.AuthorizationError):
            ingest.ingest_source("A new fact about penguins.", "peng")
        # file-back is refused for a readonly principal (no write): the PDP enforces
        # every tool uniformly now (IAM7), so it raises rather than returning a note.
        with pytest.raises(authz.AuthorizationError):
            mcp_server.mnesis_file_back("Q?", "An answer worth keeping.", 0.9)
    # A member CAN write.
    with as_principal(acme, P2):
        assert mcp_server.mnesis_file_back("What caches Atlas?", "Atlas uses Redis.", 0.9).startswith("filed digest")


def test_authorize_respects_role_capabilities_and_ownership(acme):
    private = store.Store(acme).read_page("falcon-vault")
    # Role capabilities.
    assert authz.authorize(READER, authz.READ) and not authz.authorize(READER, authz.WRITE)
    assert authz.authorize(P1, authz.WRITE) and authz.authorize(ADMIN, authz.ADMIN)
    assert not authz.authorize(P2, authz.ADMIN)  # member has no admin
    # Per-resource: read needs visibility; write needs ownership (or admin).
    assert authz.authorize(P1, authz.READ, private) and not authz.authorize(P2, authz.READ, private)
    assert authz.authorize(P1, authz.WRITE, private)          # owner
    assert not authz.authorize(P2, authz.WRITE, private)      # not owner
    assert authz.authorize(ADMIN, authz.WRITE, private)       # admin override
    # No bound principal (legacy/CLI) → everything permitted.
    assert authz.authorize(None, authz.WRITE) and authz.authorize(None, authz.ADMIN)


# ── ingest/file-back stamp owner + visibility ──────────────────────────────


def test_ingest_stamps_owner_and_tenant_default_visibility(acme):
    with as_principal(acme, P2):
        page = ingest.ingest_source("Penguins huddle for warmth in winter.", "peng")
        assert page.owner_principal == "p2" and page.visibility == "shared"  # tenant default
        # An explicit private ingest is owner-only.
        priv = ingest.ingest_source("A confidential note about Q3 numbers.", "q3", visibility="private")
        assert priv.owner_principal == "p2" and priv.visibility == "private"
    # P1 cannot see P2's private page.
    with as_principal(acme, P1):
        assert "no such page" in mcp_server.mnesis_get(priv.id).lower()


def test_per_tenant_default_visibility_is_configurable(acme):
    tenancy.TenantRegistry().set_default_visibility("acme", "private")
    with as_principal(acme, P1):
        page = ingest.ingest_source("Internal-only architecture decision.", "arch")
        assert page.visibility == "private" and page.owner_principal == "p1"
