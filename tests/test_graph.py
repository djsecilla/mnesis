"""Tests for the knowledge-graph projection and traversal (SQLite backend)."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone

import pytest

from mnesis import config, graph, store, tenancy
from mnesis.store import Page

NOW = datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def wiki(tenant):
    return tenant.root_path

def _seed_three_pages():
    # atlas -uses-> redis is asserted by TWO pages (p1, p2).
    store.write_page(Page(
        id="p1", title="Atlas uses Redis", body="b",
        tags=["project:atlas", "library:redis"],
        relations=[{"s": "project:atlas", "p": "uses", "o": "library:redis"}],
    ))
    store.write_page(Page(
        id="p2", title="Auth migration depends on Redis", body="b",
        tags=["project:atlas", "library:redis", "decision:auth-migration"],
        relations=[
            {"s": "project:atlas", "p": "uses", "o": "library:redis"},
            {"s": "decision:auth-migration", "p": "depends_on", "o": "library:redis"},
        ],
    ))
    store.write_page(Page(
        id="p3", title="Sarah owns auth migration", body="b",
        tags=["person:sarah", "decision:auth-migration"],
        relations=[{"s": "person:sarah", "p": "owns", "o": "decision:auth-migration"}],
    ))


def _edge(backend, s, p, o):
    for e in backend.get_entity(s)["edges"]:
        if (e["s"], e["p"], e["o"]) == (s, p, o):
            return e
    return None


def test_entities_and_edges_with_confidence(wiki):
    _seed_three_pages()
    summary = graph.rebuild_graph(now=NOW)
    backend = graph.get_graph_backend()

    # Entities: 4 typed + 3 page nodes.
    assert summary["entities"] == 7
    assert backend.get_entity("library:redis")["type"] == "library"

    e = _edge(backend, "project:atlas", "uses", "library:redis")
    assert e is not None
    assert e["assertion_count"] == 2  # asserted by p1 and p2
    assert sorted(e["source_pages"]) == ["p1", "p2"]
    assert 0.0 < e["confidence"] < 1.0


def test_multi_source_edge_outranks_single_source(wiki):
    _seed_three_pages()
    graph.rebuild_graph(now=NOW)
    backend = graph.get_graph_backend()

    two = _edge(backend, "project:atlas", "uses", "library:redis")  # 2 pages
    one = _edge(backend, "decision:auth-migration", "depends_on", "library:redis")  # 1 page
    assert two["confidence"] > one["confidence"]


def test_neighbors_and_depth2_traverse(wiki):
    _seed_three_pages()
    graph.rebuild_graph(now=NOW)
    backend = graph.get_graph_backend()

    # Incoming neighbors of redis: atlas (uses) and auth-migration (depends_on).
    incoming = {n["ref"] for n in backend.neighbors("library:redis", direction="in")}
    assert incoming == {"project:atlas", "decision:auth-migration"}

    # sarah -owns-> auth-migration -depends_on-> redis  (depth 2).
    reached = backend.traverse("person:sarah", depth=2)
    by_ref = {r["ref"]: r for r in reached}
    assert "decision:auth-migration" in by_ref and by_ref["decision:auth-migration"]["depth"] == 1
    assert "library:redis" in by_ref and by_ref["library:redis"]["depth"] == 2
    assert by_ref["library:redis"]["path"] == ["person:sarah", "decision:auth-migration", "library:redis"]
    assert by_ref["library:redis"]["predicates"] == ["owns", "depends_on"]


def test_stale_page_demotes_its_edge(wiki):
    _seed_three_pages()
    graph.rebuild_graph(now=NOW)
    backend = graph.get_graph_backend()
    assert backend.neighbors("person:sarah", direction="out")  # edge present while active

    # The owns-edge is supported only by p3; staling p3 should demote it.
    p3 = store.read_page("p3")
    p3.status = "stale"
    store.write_page(p3)
    graph.rebuild_graph(now=NOW)
    backend = graph.get_graph_backend()

    assert backend.neighbors("person:sarah", direction="out") == []  # excluded by default
    e = _edge(backend, "person:sarah", "owns", "decision:auth-migration")
    assert e is not None and e["demoted"] is True  # demoted, not deleted
    # Still reachable when explicitly including demoted edges.
    reached = {r["ref"] for r in backend.traverse("person:sarah", depth=1, include_demoted=True)}
    assert "decision:auth-migration" in reached


def test_cyclic_relations_do_not_hang(wiki):
    store.write_page(Page(
        id="cx", title="X depends on Y", body="b",
        tags=["concept:x", "concept:y"],
        relations=[{"s": "concept:x", "p": "depends_on", "o": "concept:y"}],
    ))
    store.write_page(Page(
        id="cy", title="Y depends on X", body="b",
        tags=["concept:x", "concept:y"],
        relations=[{"s": "concept:y", "p": "depends_on", "o": "concept:x"}],
    ))
    graph.rebuild_graph(now=NOW)
    backend = graph.get_graph_backend()

    # A generous depth must terminate (cycle-safe) and not revisit a node on a path.
    reached = backend.traverse("concept:x", depth=10)
    assert {r["ref"] for r in reached} == {"concept:y"}  # walk back to x is blocked
    for r in reached:
        assert len(r["path"]) == len(set(r["path"]))  # no node repeats within a path


def test_rebuild_reproduces_graph_identically(wiki):
    _seed_three_pages()
    graph.rebuild_graph(now=NOW)
    before = _dump(graph.get_graph_backend())

    # Delete the whole index dir; rebuild from Markdown reproduces the graph.
    import shutil

    shutil.rmtree(tenancy.current().cache_dir)
    graph.rebuild_graph(now=NOW)
    after = _dump(graph.get_graph_backend())
    assert before == after


def _dump(backend) -> list:
    """A deterministic, comparable snapshot of all edges via the interface."""
    refs = set()
    # Collect refs by walking every edge from a couple of seed entities is awkward;
    # instead snapshot via get_entity over a known entity set is insufficient, so
    # dump through neighbors of all entities reachable from the typed roots.
    roots = ["project:atlas", "decision:auth-migration", "person:sarah", "library:redis"]
    edges = []
    seen = set()
    for root in roots:
        ent = backend.get_entity(root)
        if ent is None:
            continue
        for e in ent["edges"]:
            key = (e["s"], e["p"], e["o"])
            if key in seen:
                continue
            seen.add(key)
            edges.append((
                e["s"], e["p"], e["o"], e["assertion_count"],
                tuple(e["source_pages"]), round(e["confidence"], 9), e["demoted"],
            ))
    return sorted(edges)
