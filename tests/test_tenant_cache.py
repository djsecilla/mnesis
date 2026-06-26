"""T2 — per-tenant caches, rebuild, graph, state (CLAUDE.md §16, §8).

Two tenants A and B are seeded with **overlapping topics** (both mention Redis,
both even reuse the page id ``atlas-redis``) yet are fully isolated: every derived
operation — search/query/get, entity/neighbors/traverse/impact, graph stats, decay,
the review queue, and access counts — operates strictly within the bound tenant.
Rebuilding A reconstructs only A's caches and never touches B. No operation can
surface mixed-tenant results, because each cache is a separate DB file opened from
the tenant's own ``.cache/``.
"""

from __future__ import annotations

import pytest

from mnesis import config, graph, lifecycle, search, state, store, tenancy
from mnesis.store import Page


@pytest.fixture()
def ab(tmp_path, monkeypatch):
    """Two seeded tenants (A, B) under one data root, with overlapping topics."""
    monkeypatch.setattr(config, "DATA_ROOT", tmp_path / "data", raising=False)
    monkeypatch.setattr(config, "MNESIS_LLM_STUB", True, raising=False)
    a = tenancy.create_tenant("alpha", data_root=tmp_path / "data")
    b = tenancy.create_tenant("beta", data_root=tmp_path / "data")

    with tenancy.use(a):
        # Shared id + shared entity (library:redis); A-unique entity: decision:auth-migration.
        store.write_page(Page(
            id="atlas-redis", title="Atlas uses Redis for caching",
            body="Project Atlas uses Redis as its cache.",
            sources=["a-notes"], tags=["project:atlas", "library:redis"],
            relations=[{"s": "project:atlas", "p": "uses", "o": "library:redis"}],
        ))
        store.write_page(Page(
            id="auth-a", title="The authentication migration depends on Redis",
            body="The authentication migration relies on the Redis cache.",
            sources=["a-arch"], tags=["decision:auth-migration", "library:redis"],
            relations=[{"s": "decision:auth-migration", "p": "depends_on", "o": "library:redis"}],
        ))
        search.rebuild()
        graph.rebuild_graph()

    with tenancy.use(b):
        # Same id 'atlas-redis' but B's own content; B-unique entity: project:billing.
        store.write_page(Page(
            id="atlas-redis", title="Atlas uses Redis (beta tenant copy)",
            body="Beta's Atlas page, also about Redis caching.",
            sources=["b-notes"], tags=["project:atlas", "library:redis"],
            relations=[{"s": "project:atlas", "p": "uses", "o": "library:redis"}],
        ))
        store.write_page(Page(
            id="billing-b", title="Billing depends on Redis",
            body="The billing service depends on the Redis cache for rate limits.",
            sources=["b-ops"], tags=["project:billing", "library:redis"],
            relations=[{"s": "project:billing", "p": "depends_on", "o": "library:redis"}],
        ))
        search.rebuild()
        graph.rebuild_graph()

    return a, b


# ── caches are separate files under each tenant's .cache/ ───────────────────


def test_each_tenant_has_its_own_cache_files(ab):
    a, b = ab
    assert a.cache_dir != b.cache_dir
    for name in ("wiki.db", "graph.db", "state.db"):
        # State.db is created lazily; touch it so the assertion is meaningful.
        with tenancy.use(a):
            state.record_access("atlas-redis")
        with tenancy.use(b):
            state.record_access("atlas-redis")
        af, bf = a.cache_dir / name, b.cache_dir / name
        assert af.exists() and bf.exists()
        assert af.resolve() != bf.resolve()  # never the same physical file


# ── search/query/get isolation (overlapping topic 'redis') ──────────────────


def test_search_returns_only_the_bound_tenants_pages(ab):
    a, b = ab
    with tenancy.use(a):
        a_ids = {h.id for h in search.search("redis")}
    with tenancy.use(b):
        b_ids = {h.id for h in search.search("redis")}
    assert a_ids == {"atlas-redis", "auth-a"}
    assert b_ids == {"atlas-redis", "billing-b"}
    # The shared topic never leaks the other tenant's unique page.
    assert "billing-b" not in a_ids and "auth-a" not in b_ids


def test_a_unique_topic_is_invisible_to_b_and_vice_versa(ab):
    a, b = ab
    with tenancy.use(a):
        assert {h.id for h in search.search("billing")} == set()   # B-only topic
        assert {h.id for h in search.search("authentication")} == {"auth-a"}
    with tenancy.use(b):
        assert {h.id for h in search.search("authentication")} == set()  # A-only topic
        assert {h.id for h in search.search("billing")} == {"billing-b"}


def test_get_cannot_read_the_other_tenants_page(ab):
    a, b = ab
    with tenancy.use(a):
        assert store.read_page("auth-a").title.startswith("The authentication")
        with pytest.raises(FileNotFoundError):
            store.read_page("billing-b")
        # The shared id resolves to A's OWN content, not B's.
        assert "Project Atlas uses Redis" in store.read_page("atlas-redis").body
    with tenancy.use(b):
        assert store.read_page("billing-b").title.startswith("Billing")
        with pytest.raises(FileNotFoundError):
            store.read_page("auth-a")
        assert "Beta's Atlas page" in store.read_page("atlas-redis").body


# ── graph isolation: entity / neighbors / traverse / impact / stats ─────────


def test_graph_entity_and_neighbors_are_tenant_scoped(ab):
    a, b = ab
    with tenancy.use(a):
        assert graph.entity("decision:auth-migration") is not None
        assert graph.entity("project:billing") is None            # B-only entity
        a_neighbors = {n["ref"] for n in graph.neighbors("library:redis", direction="in")}
    with tenancy.use(b):
        assert graph.entity("project:billing") is not None
        assert graph.entity("decision:auth-migration") is None    # A-only entity
        b_neighbors = {n["ref"] for n in graph.neighbors("library:redis", direction="in")}
    assert a_neighbors == {"project:atlas", "decision:auth-migration"}
    assert b_neighbors == {"project:atlas", "project:billing"}


def test_impact_never_crosses_tenants(ab):
    a, b = ab
    with tenancy.use(a):
        a_affected = {x["ref"] for x in graph.impact("library:redis")}
    with tenancy.use(b):
        b_affected = {x["ref"] for x in graph.impact("library:redis")}
    assert a_affected == {"project:atlas", "decision:auth-migration"}
    assert b_affected == {"project:atlas", "project:billing"}
    assert "project:billing" not in a_affected and "decision:auth-migration" not in b_affected


def test_traverse_and_stats_are_tenant_scoped(ab):
    a, b = ab
    with tenancy.use(a):
        a_reach = {t["ref"] for t in graph.traverse("library:redis", depth=2)}
        a_stats = graph.graph_stats()
    with tenancy.use(b):
        b_reach = {t["ref"] for t in graph.traverse("library:redis", depth=2)}
        b_stats = graph.graph_stats()
    assert "project:billing" not in a_reach
    assert "decision:auth-migration" not in b_reach
    # Each tenant's stats reflect only its own entities: A has a 'decision' node,
    # B does not (and B's edge predicates include depends_on for billing).
    assert "decision" in a_stats["entities_by_type"]
    assert "decision" not in b_stats["entities_by_type"]


# ── rebuild A leaves B untouched ────────────────────────────────────────────


def test_rebuilding_one_tenant_does_not_touch_the_other(ab):
    a, b = ab
    b_files = {n: (b.cache_dir / n) for n in ("wiki.db", "graph.db", "state.db")}
    with tenancy.use(b):
        state.record_access("billing-b")  # ensure state.db exists
    before = {n: (p.read_bytes() if p.exists() else None) for n, p in b_files.items()}

    # Drop A's caches entirely and rebuild ONLY A.
    import shutil
    shutil.rmtree(a.cache_dir, ignore_errors=True)
    with tenancy.use(a):
        search.rebuild()
        graph.rebuild_graph()
        assert {h.id for h in search.search("redis")} == {"atlas-redis", "auth-a"}

    # B's cache files are byte-for-byte unchanged, and B still returns B-only data.
    after = {n: (p.read_bytes() if p.exists() else None) for n, p in b_files.items()}
    assert after == before
    with tenancy.use(b):
        assert {h.id for h in search.search("redis")} == {"atlas-redis", "billing-b"}
        assert graph.entity("project:billing") is not None


# ── durable state (access counts + review queue) is independent ─────────────


def test_access_counts_are_independent_per_tenant(ab):
    a, b = ab
    with tenancy.use(a):
        state.record_access("atlas-redis")
        state.record_access("atlas-redis")
        assert state.get_access("atlas-redis")["count"] == 2
    with tenancy.use(b):
        # Same page id, but B's state store has never seen a read of it.
        assert state.get_access("atlas-redis") is None
        state.record_access("atlas-redis")
        assert state.get_access("atlas-redis")["count"] == 1
    with tenancy.use(a):
        assert state.get_access("atlas-redis")["count"] == 2  # unaffected by B


def test_review_queue_is_independent_per_tenant(ab):
    a, b = ab
    with tenancy.use(a):
        rid = state.enqueue_contradiction("atlas-redis", "auth-a", "conflict")
        assert [r["id"] for r in state.list_open_reviews()] == [rid]
    with tenancy.use(b):
        assert state.list_open_reviews() == []   # B's queue is empty
        state.enqueue_contradiction("atlas-redis", "billing-b", "other")
        assert len(state.list_open_reviews()) == 1
    with tenancy.use(a):
        assert len(state.list_open_reviews()) == 1  # still just A's own


def test_decay_is_tenant_scoped(ab):
    a, b = ab
    # A decay pass over A recomputes only A's pages; B is never read or transitioned.
    with tenancy.use(a):
        report_a = lifecycle.recompute_all()
    with tenancy.use(b):
        report_b = lifecycle.recompute_all()
    # Each pass saw exactly its own two pages (no cross-tenant bleed).
    assert report_a["scanned"] == 2 and report_b["scanned"] == 2
