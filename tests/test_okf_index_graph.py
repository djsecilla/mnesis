"""OKF4 — index, cache, and graph rebuilt from OKF entries (no regressions).

The rebuildable caches (FTS index + knowledge graph) read OKF documents via the same
`Page` abstraction, so a migrated OKF corpus yields **equivalent** search/query/get/
graph/traverse/impact results to the pre-OKF corpus. OKF cross-links populate the graph
(reconciled with the richer typed relations), and rebuild is deterministic + per-tenant.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone

import pytest

from mnesis import config, graph, okf, search, store, tenancy
from mnesis.store import Page

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)  # fixed clock → deterministic graph conf

# A pre-OKF corpus (old frontmatter shape) exercising tags, typed relations (uses +
# depends_on for impact), and a supersession chain.
def _old(id, title, body, *, kind="fact", status="active", tags=(), relations=(),
         supersedes=None, superseded_by=None, sources=("s",)):
    import yaml
    meta = {
        "id": id, "title": title, "created": "2026-01-01T00:00:00.000000Z",
        "updated": "2026-02-02T00:00:00.000000Z", "sources": list(sources), "source_count": 1,
        "last_confirmed": "2026-02-02T00:00:00.000000Z", "tags": list(tags), "kind": kind,
        "status": status, "owner_principal": None, "visibility": "shared",
        "supersedes": supersedes, "superseded_by": superseded_by, "contradicts": [],
        "decay_class": None, "relations": list(relations),
    }
    return f"---\n{yaml.safe_dump(meta, sort_keys=False)}---\n{body}\n"


CORPUS = {
    "atlas.md": _old("atlas", "Project Atlas uses Redis for caching",
                     "Project Atlas uses Redis as its primary caching layer.",
                     tags=["project:atlas", "library:redis"],
                     relations=[{"s": "project:atlas", "p": "uses", "o": "library:redis"}]),
    "authmig.md": _old("authmig", "Auth migration depends on Redis",
                       "The auth migration relies on the Redis cache.",
                       tags=["decision:auth-migration", "library:redis"],
                       relations=[{"s": "decision:auth-migration", "p": "depends_on", "o": "library:redis"}]),
    "legacy.md": _old("legacy", "Legacy Redis note", "An old note about Redis.",
                      status="stale", superseded_by="atlas", tags=["library:redis"]),
}


def _seed_old(ctx, files: dict[str, str]) -> None:
    ctx.pages_dir.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (ctx.pages_dir / name).write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", str(ctx.root_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(ctx.root_path), "commit", "-q", "-m", "seed pre-OKF"], check=True)


def _snapshot() -> dict:
    """Capture the observable results the acceptance regression-compares."""
    search.rebuild()
    graph.rebuild_graph(now=NOW)
    backend = graph.get_graph_backend()
    a = store.read_page("atlas")
    return {
        "query_redis": [h.id for h in search.search("redis")],
        "query_caching": [h.id for h in search.search("caching")],
        "get_atlas": (a.title, a.body, a.kind, a.status, a.relations),
        "edges": sorted((e["s"], e["p"], e["o"], round(e["confidence"], 6)) for e in backend.all_edges()),
        "stats": graph.graph_stats(),
        "neighbors_redis": sorted(n["ref"] for n in graph.neighbors("library:redis", direction="both")),
        "traverse_atlas": sorted(tuple(r["path"]) for r in graph.traverse("project:atlas", depth=2)),
        "impact_redis": sorted((a["ref"], a["hop"]) for a in graph.impact("library:redis")),
    }


# ── no regression: migrated OKF corpus == pre-OKF corpus ───────────────────


def test_no_regression_across_migration(tenant):
    _seed_old(tenant, CORPUS)
    before = _snapshot()                       # pre-OKF (old shape, via the tolerant reader)

    store.migrate_to_okf()                     # rewrite to OKF
    assert okf.validate_bundle(tenant.pages_dir).conformant

    after = _snapshot()                        # post-OKF
    # Search, get, graph, neighbors, traverse, impact — all equivalent.
    assert before["query_redis"] == after["query_redis"] and before["query_redis"]
    assert before["query_caching"] == after["query_caching"]
    assert before["get_atlas"] == after["get_atlas"]
    assert before["edges"] == after["edges"]
    assert before["stats"] == after["stats"]
    assert before["neighbors_redis"] == after["neighbors_redis"]
    assert before["traverse_atlas"] == after["traverse_atlas"]
    assert before["impact_redis"] == after["impact_redis"]
    # The specific pre-OKF expectations still hold.
    assert ("project:atlas", "uses", "library:redis") in {(e[0], e[1], e[2]) for e in after["edges"]}
    assert after["impact_redis"] == [("decision:auth-migration", 1), ("project:atlas", 1)]


# ── the OKF `type` is indexed + searchable, without perturbing content ranking ──


def test_type_is_indexed(tenant):
    _seed_old(tenant, CORPUS)
    store.migrate_to_okf()
    search.rebuild()
    # `type` (= kind) is searchable, and content queries are unchanged by it.
    assert {"atlas", "authmig"} <= set(h.id for h in search.search("fact"))
    assert "atlas" in [h.id for h in search.search("redis")]


# ── OKF cross-links populate the graph (reconciled with typed relations) ───


def test_cross_links_become_graph_edges(tenant):
    _seed_old(tenant, CORPUS)
    store.migrate_to_okf()

    # (a) Mnesis's own generated cross-links mirror the typed relation — the doc links
    #     the same concepts that the typed edge connects (consistency, not duplication).
    raw = (tenant.pages_dir / "atlas.md").read_text(encoding="utf-8")
    links = okf.cross_links(raw)
    assert "/project/atlas" in links and "/library/redis" in links
    graph.rebuild_graph(now=NOW)
    edges = {(e["s"], e["p"], e["o"]) for e in graph.get_graph_backend().all_edges()}
    assert ("project:atlas", "uses", "library:redis") in edges  # the typed (richer) edge
    assert not any(p == "related_to" for _, p, _ in edges)      # no redundant related_to for Mnesis pages

    # (b) A hand-authored OKF-native page with a prose cross-link (NO typed relation)
    #     becomes a navigable `related_to` edge.
    ext = Page(id="runbook", title="Ops runbook", kind="note",
               body="The runbook depends on [redis](/library/redis).")
    store.write_page(ext)
    graph.rebuild_graph(now=NOW)
    edges2 = {(e["s"], e["p"], e["o"]) for e in graph.get_graph_backend().all_edges()}
    assert any(p == "related_to" and "library:redis" in (s, o) and "page:runbook" in (s, o)
               for s, p, o in edges2)
    # OKF-navigability: redis's neighbourhood now reaches the runbook.
    assert "page:runbook" in [n["ref"] for n in graph.neighbors("library:redis", direction="both")]
    # …but impact() is unchanged (related_to does not widen depends_on/uses reachability).
    assert sorted(a["ref"] for a in graph.impact("library:redis")) == \
        ["decision:auth-migration", "project:atlas"]


# ── rebuild is deterministic and per-tenant ────────────────────────────────


def test_rebuild_is_deterministic(tenant):
    _seed_old(tenant, CORPUS)
    store.migrate_to_okf()
    e1 = sorted((e["s"], e["p"], e["o"], round(e["confidence"], 6))
                for e in (graph.rebuild_graph(now=NOW), graph.get_graph_backend().all_edges())[1])
    e2 = sorted((e["s"], e["p"], e["o"], round(e["confidence"], 6))
                for e in (graph.rebuild_graph(now=NOW), graph.get_graph_backend().all_edges())[1])
    assert e1 == e2
    assert search.rebuild() == search.rebuild()  # same page count, idempotent


def test_index_and_graph_are_per_tenant(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_ROOT", tmp_path / "data", raising=False)
    monkeypatch.setattr(config, "MNESIS_LLM_STUB", True, raising=False)
    a = tenancy.create_tenant("alpha", data_root=config.DATA_ROOT)
    b = tenancy.create_tenant("beta", data_root=config.DATA_ROOT)
    with tenancy.use(a):
        _seed_old(a, {"atlas.md": CORPUS["atlas.md"]})
        store.migrate_to_okf()
        search.rebuild(); graph.rebuild_graph(now=NOW)
    with tenancy.use(b):
        _seed_old(b, {"authmig.md": CORPUS["authmig.md"]})
        search.rebuild(); graph.rebuild_graph(now=NOW)

    # Each tenant's caches see ONLY its own knowledge — no cross-contamination.
    with tenancy.use(a):
        assert search.indexed_ids() == {"atlas"}
        assert "project:atlas" in [e["ref"] for e in graph.get_graph_backend().all_entities()]
        assert "decision:auth-migration" not in [e["ref"] for e in graph.get_graph_backend().all_entities()]
    with tenancy.use(b):
        assert search.indexed_ids() == {"authmig"}
        assert "decision:auth-migration" in [e["ref"] for e in graph.get_graph_backend().all_entities()]
        assert "project:atlas" not in [e["ref"] for e in graph.get_graph_backend().all_entities()]
