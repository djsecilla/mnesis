"""Tests for graph-augmented query and impact() (stub mode).

Corpus: the auth-migration page declares ``depends_on library:redis`` in its
relations but NEVER mentions the word "redis" in its searchable fields — so only
the graph can connect a Redis query to it. Atlas depends on the auth migration,
giving a two-hop chain.
"""

from __future__ import annotations

import subprocess

import pytest

from mnesis import config, graph, search, store
from mnesis.store import Page


@pytest.fixture()
def wiki(tmp_path, monkeypatch):
    root = tmp_path / "wiki"
    (root / "pages").mkdir(parents=True)
    monkeypatch.setattr(config, "MNESIS_ROOT", root)
    monkeypatch.setattr(config, "PAGES_DIR", root / "pages")
    monkeypatch.setattr(config, "INDEX_DIR", root / ".index")
    monkeypatch.setattr(config, "GRAPH_BACKEND", "sqlite")
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@localhost"], check=True
    )
    # The page that actually mentions Redis (BM25 anchor + declares the entity).
    store.write_page(Page(
        id="redis-cache", title="Redis is the caching layer",
        body="Redis is an in-memory cache used widely.", tags=["library:redis"],
    ))
    # Declares the depends_on edge but never says "redis" in title/body/tags.
    store.write_page(Page(
        id="auth-mig", title="The authentication migration",
        body="The authentication migration is owned by Sarah.",
        tags=["decision:auth-migration", "person:sarah"],
        relations=[{"s": "decision:auth-migration", "p": "depends_on", "o": "library:redis"}],
    ))
    # Atlas depends on the auth migration (two-hop chain to Redis).
    store.write_page(Page(
        id="atlas", title="Project Atlas overview",
        body="Project Atlas relies on the authentication migration.",
        tags=["project:atlas"],
        relations=[{"s": "project:atlas", "p": "depends_on", "o": "decision:auth-migration"}],
    ))
    search.rebuild()
    graph.rebuild_graph()
    return tmp_path


def test_query_surfaces_graph_reachable_page_with_grounding(wiki):
    hits = graph.graph_query("redis")
    by_id = {h.id: h for h in hits}

    # Keyword anchor present...
    assert "redis-cache" in by_id
    # ...and the auth-migration page surfaces via the graph, though it has no "redis" word.
    assert "auth-mig" in by_id
    auth = by_id["auth-mig"]
    assert auth.bm25_score == 0.0  # not a keyword match
    assert auth.graph_proximity > 0.0
    assert auth.grounding is not None
    edge = auth.grounding["edge"]
    assert (edge["s"], edge["p"], edge["o"]) == (
        "decision:auth-migration", "depends_on", "library:redis"
    )
    assert auth.grounding["source_page"] == "auth-mig"  # grounded in a real page


def test_plain_keyword_query_with_no_entity_is_unchanged(wiki):
    # "caching" matches a page but resolves to no entity (no entity value has that token).
    augmented = graph.graph_query("caching")
    base = search.search("caching")
    assert [h.id for h in augmented] == [h.id for h in base]
    assert all(h.grounding is None and h.graph_proximity == 0.0 for h in augmented)


def test_impact_reverse_traverses_depends_on_and_uses(wiki):
    affected = graph.impact("library:redis", depth=3)
    by_ref = {a["ref"]: a for a in affected}

    # auth-migration depends_on redis directly...
    assert "decision:auth-migration" in by_ref
    assert by_ref["decision:auth-migration"]["hop"] == 1
    assert by_ref["decision:auth-migration"]["path"] == [
        "decision:auth-migration", "library:redis"
    ]
    # ...and Atlas transitively (Atlas depends_on auth-migration depends_on redis).
    assert "project:atlas" in by_ref
    assert by_ref["project:atlas"]["hop"] == 2
    assert by_ref["project:atlas"]["path"] == [
        "project:atlas", "decision:auth-migration", "library:redis"
    ]
    assert by_ref["project:atlas"]["grounding_pages"] == ["atlas"]


def test_impact_excludes_demoted_edges(wiki):
    # Stale the auth-migration page -> its depends_on edge demotes -> drops from impact.
    p = store.read_page("auth-mig")
    p.status = "stale"
    store.write_page(p)
    graph.rebuild_graph()

    affected = {a["ref"] for a in graph.impact("library:redis", depth=3)}
    assert affected == set()  # the only path ran through the now-demoted edge
