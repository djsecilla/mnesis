"""Phase-3 end-to-end regression: graph projection, impact, supersession, and the
rebuildable-cache invariant for the graph (stub mode, deterministic clock)."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone

import pytest

from mnesis import config, graph, ingest, search, state, store, tenancy

NOW = datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def wiki(tenant):
    return tenant.root_path

def _seed_chain():
    """Atlas -> auth-migration -> Redis, plus Sarah owns the migration."""
    a = ingest.ingest_source(
        "Project Atlas depends on the authentication migration. "
        "tag{project:atlas} tag{decision:auth-migration} "
        "rel{project:atlas|depends_on|decision:auth-migration}",
        "atlas-overview",
    )
    ingest.ingest_source(
        "The authentication migration depends on the Redis cache. "
        "tag{decision:auth-migration} tag{library:redis} "
        "rel{decision:auth-migration|depends_on|library:redis}",
        "auth-notes",
    )
    ingest.ingest_source(
        "Sarah owns the authentication migration. "
        "tag{person:sarah} tag{decision:auth-migration} "
        "rel{person:sarah|owns|decision:auth-migration}",
        "ownership",
    )
    return a


def _rebuild():
    search.rebuild()
    return graph.rebuild_graph(now=NOW)


def _dump_graph() -> dict:
    backend = graph.get_graph_backend()
    edges = sorted(
        (e["s"], e["p"], e["o"], e["assertion_count"], tuple(e["source_pages"]),
         round(e["confidence"], 12), e["demoted"])
        for e in backend.all_edges()
    )
    entities = sorted((e["ref"], e["type"]) for e in backend.all_entities())
    return {"edges": edges, "entities": entities}


def test_phase3_graph_lifecycle(wiki):
    # Steps 1-2: ingest chain and build the graph.
    atlas = _seed_chain()
    summary = _rebuild()
    assert (summary["entities"], summary["edges"], summary["demoted"]) == (7, 3, 0)

    # The Atlas page genuinely never mentions Redis (only the graph connects them).
    assert "redis" not in atlas.body.lower()
    assert "library:redis" not in atlas.tags

    # Step 3: impact of Redis reaches auth-migration (hop 1) and Atlas (hop 2).
    imp = {a["ref"]: a for a in graph.impact("library:redis")}
    assert imp["decision:auth-migration"]["hop"] == 1
    assert imp["project:atlas"]["hop"] == 2
    assert imp["project:atlas"]["path"] == [
        "project:atlas", "decision:auth-migration", "library:redis"
    ]

    # Step 4: a superseding source moves the dependency to Postgres.
    ingest.ingest_source(
        "The authentication migration depends on the Redis cache. relation:supersedes "
        "The migration now uses Postgres instead. "
        "tag{decision:auth-migration} tag{library:postgres} "
        "rel{decision:auth-migration|depends_on|library:postgres}",
        "auth-postgres",
    )
    _rebuild()

    # The superseding page's edge took over; the superseded edge is demoted.
    edges = {(e["s"], e["p"], e["o"]): e for e in graph.get_graph_backend().all_edges()}
    assert edges[("decision:auth-migration", "depends_on", "library:redis")]["demoted"] is True
    assert edges[("decision:auth-migration", "depends_on", "library:postgres")]["demoted"] is False
    # Impact follows: Redis no longer affects anything; Postgres now carries the chain.
    assert graph.impact("library:redis") == []
    assert {a["ref"] for a in graph.impact("library:postgres")} == {
        "decision:auth-migration", "project:atlas"
    }


def test_caches_rebuild_reproduces_graph_and_preserves_state(wiki):
    atlas = _seed_chain()
    _rebuild()

    # Durable state: an access record and an open review.
    state.record_access(atlas.id)
    state.record_access(atlas.id)
    review_id = state.enqueue_contradiction(atlas.id, "ghost", "fixture review")

    # Rebuild so the caches reflect the durable state, then snapshot.
    _rebuild()
    graph_before = _dump_graph()
    search_before = [
        (h.id, round(h.confidence, 4)) for h in search.search("authentication", include_stale=True)
    ]
    access_before = state.get_access(atlas.id)
    reviews_before = state.list_open_reviews()

    # Delete ONLY the rebuildable caches (search index + graph); keep state.db.
    (tenancy.current().cache_dir / "wiki.db").unlink()
    (tenancy.current().cache_dir / "graph.db").unlink()
    assert (tenancy.current().cache_dir / "state.db").exists()
    _rebuild()

    # The graph is reproduced exactly (deterministic clock).
    assert _dump_graph() == graph_before
    # Search ranking reproduced; confidences within tolerance.
    search_after = [
        (h.id, round(h.confidence, 4)) for h in search.search("authentication", include_stale=True)
    ]
    assert [x[0] for x in search_after] == [x[0] for x in search_before]
    for (_, cb), (_, ca) in zip(search_before, search_after):
        assert abs(ca - cb) < 0.01
    # Durable state survived untouched.
    assert state.get_access(atlas.id) == access_before
    assert state.list_open_reviews() == reviews_before
    assert any(r["id"] == review_id for r in state.list_open_reviews())
