"""V4 — per-vault caches, graph, state, rebuild (CLAUDE.md §16 Vaults, §8).

With per-vault stores + config, every DERIVED store is per-vault too: the search index,
the graph backend, and state.db (access counts + review queue) live under the vault root
and open from the VaultContext, and every derived op honours the vault's own schema. Two
vaults of one tenant with overlapping topics but different schemas must never leak into
one another: A's search/graph/traverse return only A's data under A's schema; rebuilding A
leaves B's caches byte-for-byte untouched; A's access counts and review queue are wholly
independent of B's.
"""

from __future__ import annotations

import pytest

from mnesis import config, graph, ingest, search, state, store, tenancy, vocab

# Overlapping topic (both are about Atlas + Redis) but distinct content + schemas.
SRC_A = (
    "Atlas uses Redis for caching in vault Alpha. "
    "rel{project:atlas|uses|library:redis} "
    "rel{org:acme|employs|person:bob} "
    "tag{library:redis} tag{org:acme}"
)
SRC_B = (
    "Atlas uses Redis for caching in vault Beta. "
    "rel{project:atlas|uses|library:redis} "
    "tag{library:redis}"
)


@pytest.fixture()
def vaults(tmp_path, monkeypatch):
    """Tenant ``acme`` with two seeded vaults: ``alpha`` (schema adds org/employs) and
    ``beta`` (the default schema). Each is ingested + indexed + graphed in isolation."""
    root = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_ROOT", root, raising=False)
    monkeypatch.setattr(config, "MNESIS_LLM_STUB", True, raising=False)
    tenancy.create_tenant("acme", data_root=root)
    A = tenancy.create_vault("acme", "alpha", data_root=root)
    B = tenancy.create_vault("acme", "beta", data_root=root)
    vocab.save_config(
        A, vocab.VaultConfig(entity_types=("person", "project", "library", "org"), predicates=("uses", "employs"))
    )
    with tenancy.use(A):
        pa = ingest.ingest_source(SRC_A, "src")
        search.rebuild()
        graph.rebuild_graph()
    with tenancy.use(B):
        pb = ingest.ingest_source(SRC_B, "src")
        search.rebuild()
        graph.rebuild_graph()
    return root, A, B, pa, pb


# ── the caches physically live under each vault root ────────────────────────


def test_caches_are_per_vault_on_disk(vaults):
    root, A, B, pa, pb = vaults
    for ctx in (A, B):
        for name in ("wiki.db", "graph.db", "state.db"):
            assert ctx.cache_path(name).is_relative_to(ctx.root_path)
            assert ctx.cache_path(name).exists()
    # No shared cache: A's and B's cache dirs are disjoint.
    assert A.cache_dir != B.cache_dir
    assert A.cache_dir.parent != B.cache_dir.parent  # different vault roots


# ── search is vault-scoped: A never surfaces B's data ───────────────────────


def test_search_is_vault_scoped(vaults):
    root, A, B, pa, pb = vaults
    assert pa.id != pb.id
    with tenancy.use(A):
        ids = {h.id for h in search.search("redis")}
        assert pa.id in ids and pb.id not in ids  # only A's page
    with tenancy.use(B):
        ids = {h.id for h in search.search("redis")}
        assert pb.id in ids and pa.id not in ids  # only B's page


# ── graph/traverse are vault-scoped AND schema-aware ────────────────────────


def test_graph_and_traverse_are_vault_scoped_and_schema_aware(vaults):
    root, A, B, pa, pb = vaults
    with tenancy.use(A):
        # A's schema admits org/employs — the node + edge exist.
        assert graph.entity("org:acme") is not None
        emp = [n for n in graph.neighbors("org:acme") if n["predicate"] == "employs"]
        assert emp and emp[0]["ref"] == "person:bob"
        assert graph.entity("library:redis") is not None
    with tenancy.use(B):
        # B's default schema has no `org`/`employs`; that relation was dropped, so no
        # org node and B's graph never contains A's person:bob.
        assert graph.entity("org:acme") is None
        assert graph.entity("person:bob") is None
        # B has its own library:redis node (overlapping topic) but reaches no A data.
        assert graph.entity("library:redis") is not None
        reached = {node for t in graph.traverse("library:redis", depth=3) for node in t["path"]}
        assert "person:bob" not in reached and "org:acme" not in reached


# ── rebuilding A leaves B untouched (cannot cross vault roots) ───────────────


def test_rebuilding_one_vault_leaves_the_other_untouched(vaults):
    root, A, B, pa, pb = vaults
    with tenancy.use(B):
        before_ids = sorted(h.id for h in search.search("redis"))
        before_redis = graph.entity("library:redis") is not None
    b_wiki_mtime = B.cache_path("wiki.db").stat().st_mtime_ns
    b_graph_mtime = B.cache_path("graph.db").stat().st_mtime_ns

    # Rebuild A from scratch — its own store + schema only.
    with tenancy.use(A):
        search.rebuild()
        graph.rebuild_graph()

    # B's cache files were not opened for write, and B's results are unchanged.
    assert B.cache_path("wiki.db").stat().st_mtime_ns == b_wiki_mtime
    assert B.cache_path("graph.db").stat().st_mtime_ns == b_graph_mtime
    with tenancy.use(B):
        assert sorted(h.id for h in search.search("redis")) == before_ids
        assert (graph.entity("library:redis") is not None) == before_redis
        assert graph.entity("org:acme") is None  # A's schema/data never leaked in


# ── state (access counts + review queue) is per-vault ───────────────────────


def test_access_counts_are_per_vault(vaults):
    root, A, B, pa, pb = vaults
    with tenancy.use(A):
        state.record_access(pa.id)
        state.record_access(pa.id)
        assert state.get_access(pa.id)["count"] >= 2
    with tenancy.use(B):
        assert state.get_access(pa.id) is None  # A's reads invisible in B
        state.record_access(pb.id)
        assert state.get_access(pb.id)["count"] == 1
    with tenancy.use(A):
        assert state.get_access(pb.id) is None  # B's reads invisible in A


def test_review_queues_are_independent(vaults):
    root, A, B, pa, pb = vaults
    with tenancy.use(A):
        state.enqueue_contradiction(pa.id, "ghost-a", "conflict in A")
        assert len(state.list_open_reviews()) == 1
    with tenancy.use(B):
        assert state.list_open_reviews() == []  # B's queue is its own
        state.enqueue_contradiction(pb.id, "ghost-b", "conflict in B")
        assert len(state.list_open_reviews()) == 1
    with tenancy.use(A):
        reviews = state.list_open_reviews()
        assert len(reviews) == 1 and reviews[0]["detail"] == "conflict in A"


# ── no query, on any surface, returns mixed-vault results ───────────────────


def test_no_query_returns_mixed_vault_results(vaults):
    root, A, B, pa, pb = vaults
    with tenancy.use(A):
        assert all(h.id != pb.id for h in search.search("atlas"))
        assert graph.entity("person:bob") is not None  # A owns this node
        assert "Alpha" in store.read_page(pa.id).title
    with tenancy.use(B):
        assert all(h.id != pa.id for h in search.search("atlas"))
        assert "Beta" in store.read_page(pb.id).title
        with pytest.raises(FileNotFoundError):
            store.read_page(pa.id)  # A's canonical page is unreachable from B
