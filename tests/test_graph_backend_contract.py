"""GraphBackend contract: the core assertions, run through the INTERFACE only.

These tests never reference a concrete backend class — they go through
``get_graph_backend()`` (typed as ``GraphBackend``) and the abstract methods. A
second backend (Postgres+AGE, Neo4j, ...) can be dropped in via config and must
pass this file unchanged.
"""

from __future__ import annotations

import pytest

from mnesis import config, graph, tenancy


@pytest.fixture()
def backend(tenant):
    b = graph.get_graph_backend()
    assert isinstance(b, graph.GraphBackend)  # we test the interface, not a class
    return b


def _build(b):
    b.clear()
    for ref, t in [
        ("project:atlas", "project"),
        ("library:redis", "library"),
        ("concept:caching", "concept"),
        ("person:sarah", "person"),
        ("decision:auth-migration", "decision"),
    ]:
        b.add_entity(ref, t)
    # atlas -uses-> redis asserted by two active pages.
    b.add_edge("project:atlas", "uses", "library:redis", "p1", 0.5, True)
    b.add_edge("project:atlas", "uses", "library:redis", "p2", 0.5, True)
    # redis -depends_on-> caching by one active page (for a depth-2 chain).
    b.add_edge("library:redis", "depends_on", "concept:caching", "p2", 0.5, True)
    # sarah -owns-> auth-migration asserted ONLY by a stale page -> demoted.
    b.add_edge("person:sarah", "owns", "decision:auth-migration", "p3", 0.4, False)
    b.finalize()


def test_get_entity_and_noisy_or_and_assertion_count(backend):
    _build(backend)
    ent = backend.get_entity("project:atlas")
    assert ent["type"] == "project"

    uses = next(e for e in ent["edges"] if e["p"] == "uses")
    assert uses["assertion_count"] == 2
    assert sorted(uses["source_pages"]) == ["p1", "p2"]
    # noisy-OR of 0.5 and 0.5 = 1 - 0.5*0.5 = 0.75.
    assert uses["confidence"] == pytest.approx(0.75)
    assert 0.0 < uses["confidence"] < 1.0


def test_two_sources_outrank_one(backend):
    _build(backend)
    uses = next(e for e in backend.get_entity("project:atlas")["edges"] if e["p"] == "uses")
    depends = next(
        e for e in backend.get_entity("library:redis")["edges"] if e["p"] == "depends_on"
    )
    assert uses["assertion_count"] == 2 and depends["assertion_count"] == 1
    assert uses["confidence"] > depends["confidence"]


def test_neighbors_directions(backend):
    _build(backend)
    out = {n["ref"] for n in backend.neighbors("library:redis", direction="out")}
    inn = {n["ref"] for n in backend.neighbors("library:redis", direction="in")}
    both = {n["ref"] for n in backend.neighbors("library:redis", direction="both")}
    assert out == {"concept:caching"}
    assert inn == {"project:atlas"}
    assert both == {"concept:caching", "project:atlas"}
    # Predicate filter.
    assert backend.neighbors("library:redis", predicate="owns", direction="out") == []


def test_depth2_traverse_paths(backend):
    _build(backend)
    reached = {r["ref"]: r for r in backend.traverse("project:atlas", depth=2)}
    assert set(reached) == {"library:redis", "concept:caching"}
    assert reached["concept:caching"]["path"] == ["project:atlas", "library:redis", "concept:caching"]
    assert reached["concept:caching"]["predicates"] == ["uses", "depends_on"]
    # Depth bound respected.
    assert {r["ref"] for r in backend.traverse("project:atlas", depth=1)} == {"library:redis"}


def test_demoted_edge_excluded_by_default(backend):
    _build(backend)
    assert backend.neighbors("person:sarah", direction="out") == []
    owns = next(e for e in backend.get_entity("person:sarah")["edges"] if e["p"] == "owns")
    assert owns["demoted"] is True
    # Visible only when explicitly included.
    incl = {r["ref"] for r in backend.traverse("person:sarah", depth=1, include_demoted=True)}
    assert incl == {"decision:auth-migration"}


def test_traversal_is_cycle_safe(backend):
    backend.clear()
    backend.add_entity("concept:x", "concept")
    backend.add_entity("concept:y", "concept")
    backend.add_edge("concept:x", "depends_on", "concept:y", "p1", 0.7, True)
    backend.add_edge("concept:y", "depends_on", "concept:x", "p2", 0.7, True)
    backend.finalize()

    reached = backend.traverse("concept:x", depth=10)  # must terminate
    # Cycle-safe: y is reached; the walk back to x is blocked (x already on path).
    assert {r["ref"] for r in reached} == {"concept:y"}
    for r in reached:
        assert len(r["path"]) == len(set(r["path"]))  # no repeats within a path


def test_symmetric_edges_collapse_and_traverse_both_ways(backend):
    # A symmetric predicate (related_to) asserted in BOTH directions by two pages
    # collapses to ONE undirected edge, traversable from either endpoint.
    backend.clear()
    backend.add_entity("concept:a", "concept")
    backend.add_entity("concept:b", "concept")
    backend.add_edge("concept:a", "related_to", "concept:b", "p1", 0.5, True)
    backend.add_edge("concept:b", "related_to", "concept:a", "p2", 0.5, True)  # reciprocal
    backend.finalize()

    # Collapsed: a single edge with both pages as provenance.
    edges = backend.all_edges()
    rel = [e for e in edges if e["p"] == "related_to"]
    assert len(rel) == 1
    assert rel[0]["symmetric"] is True
    assert rel[0]["assertion_count"] == 2
    assert sorted(rel[0]["source_pages"]) == ["p1", "p2"]

    # Reachable from either endpoint, and reported as undirected.
    a_nb = backend.neighbors("concept:a", direction="out")
    b_nb = backend.neighbors("concept:b", direction="out")  # b is the non-canonical end
    assert {n["ref"] for n in a_nb} == {"concept:b"}
    assert {n["ref"] for n in b_nb} == {"concept:a"}  # symmetric: found despite "out"
    assert a_nb[0]["direction"] == "both" and b_nb[0]["direction"] == "both"

    # traverse follows the undirected edge from both ends.
    assert {r["ref"] for r in backend.traverse("concept:a", depth=1)} == {"concept:b"}
    assert {r["ref"] for r in backend.traverse("concept:b", depth=1)} == {"concept:a"}


def test_directed_edges_keep_direction(backend):
    # A directed predicate is unaffected: only reachable "out" from the subject.
    backend.clear()
    backend.add_entity("project:atlas", "project")
    backend.add_entity("library:redis", "library")
    backend.add_edge("project:atlas", "uses", "library:redis", "p1", 0.6, True)
    backend.finalize()

    assert {n["ref"] for n in backend.neighbors("project:atlas", direction="out")} == {"library:redis"}
    assert backend.neighbors("library:redis", direction="out") == []  # redis doesn't "use" atlas
    redis_in = backend.neighbors("library:redis", direction="in")
    assert {n["ref"] for n in redis_in} == {"project:atlas"} and redis_in[0]["direction"] == "in"
