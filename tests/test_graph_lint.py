"""Tests for graph lint: detection, safe auto-fix, idempotency (stub mode)."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone

import pytest

from mnesis import config, graph, graph_lint, store
from mnesis.store import Page

NOW = datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def wiki(tmp_path, monkeypatch):
    root = tmp_path / "wiki"
    (root / "pages").mkdir(parents=True)
    monkeypatch.setattr(config, "WIKI_ROOT", root)
    monkeypatch.setattr(config, "PAGES_DIR", root / "pages")
    monkeypatch.setattr(config, "INDEX_DIR", root / ".index")
    monkeypatch.setattr(config, "GRAPH_BACKEND", "sqlite")
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@localhost"], check=True
    )
    return tmp_path


def _seed_corpus():
    # Healthy edge atlas -uses-> redis, declared on both ends.
    store.write_page(Page(
        id="atlas", title="Atlas uses Redis", body="b",
        tags=["project:atlas", "library:redis"],
        relations=[{"s": "project:atlas", "p": "uses", "o": "library:redis"}],
    ))
    # UNDECLARED entity: concept:scaling appears in a relation but no page tags it.
    store.write_page(Page(
        id="scaling", title="Redis enables scaling", body="b",
        tags=["library:redis"],  # note: concept:scaling intentionally NOT tagged
        relations=[{"s": "library:redis", "p": "uses", "o": "concept:scaling"}],
    ))
    # ORPHAN entity: person:nobody declared as a tag but used in no relation.
    store.write_page(Page(
        id="orphan-page", title="A note", body="b", tags=["person:nobody"], relations=[],
    ))
    # STALE-ONLY edge will come from this page once we stale it.
    store.write_page(Page(
        id="auth", title="Auth depends on Redis", body="b",
        tags=["decision:auth-migration", "library:redis"],
        relations=[{"s": "decision:auth-migration", "p": "depends_on", "o": "library:redis"}],
    ))


def test_detects_categories_report_only(wiki):
    _seed_corpus()
    graph.rebuild_graph(now=NOW)

    report = graph_lint.graph_lint(fix=False, now=NOW)

    assert [u["ref"] for u in report.undeclared_entities] == ["concept:scaling"]
    assert report.undeclared_entities[0]["suggested_pages"] == ["scaling"]
    assert "person:nobody" in {o["ref"] for o in report.orphan_entities}
    # Report-only: nothing mutated — the depends_on edge is still NOT demoted.
    edges = {(e["s"], e["p"], e["o"]): e for e in graph.get_graph_backend().all_edges()}
    assert edges[("decision:auth-migration", "depends_on", "library:redis")]["demoted"] is False


def test_fix_demotes_stale_only_edge_and_recomputes_confidence(wiki):
    _seed_corpus()
    graph.rebuild_graph(now=NOW)

    # Stale the auth page AFTER the rebuild, so the cache is now out of date.
    p = store.read_page("auth")
    p.status = "stale"
    store.write_page(p)

    report = graph_lint.graph_lint(fix=True, now=NOW)

    # The auth->redis edge was the only stale-only one and got demoted.
    triples = {tuple(e["triple"]) for e in report.stale_only_edges}
    assert ("decision:auth-migration", "depends_on", "library:redis") in triples
    assert report.confidence_updates  # at least one edge's confidence changed

    edges = {(e["s"], e["p"], e["o"]): e for e in graph.get_graph_backend().all_edges()}
    assert edges[("decision:auth-migration", "depends_on", "library:redis")]["demoted"] is True


def test_fix_is_idempotent(wiki):
    _seed_corpus()
    graph.rebuild_graph(now=NOW)
    p = store.read_page("auth")
    p.status = "stale"
    store.write_page(p)

    first = graph_lint.graph_lint(fix=True, now=NOW)
    assert first.changes > 0  # the first run actually fixed something

    second = graph_lint.graph_lint(fix=True, now=NOW)
    assert second.changes == 0  # no-op: nothing left to fix


def test_flagged_categories_are_never_auto_deleted(wiki):
    _seed_corpus()
    graph.rebuild_graph(now=NOW)
    backend = graph.get_graph_backend()
    entities_before = {e["ref"] for e in backend.all_entities()}
    edges_before = {(e["s"], e["p"], e["o"]) for e in backend.all_edges()}

    graph_lint.graph_lint(fix=True, now=NOW)

    backend = graph.get_graph_backend()
    entities_after = {e["ref"] for e in backend.all_entities()}
    edges_after = {(e["s"], e["p"], e["o"]) for e in backend.all_edges()}

    # Undeclared/orphan entities and their edges are flagged, never removed.
    assert "concept:scaling" in entities_after
    assert "person:nobody" in entities_after
    assert entities_before == entities_after
    assert edges_before == edges_after  # no edge deleted (only demoted/recomputed)


def test_dangling_structural_edge_flagged(wiki):
    store.write_page(Page(
        id="newer", title="Newer page", body="b", tags=["project:atlas"],
        supersedes="ghost-page",  # points at a page that does not exist
    ))
    graph.rebuild_graph(now=NOW)

    report = graph_lint.graph_lint(fix=False, now=NOW)
    assert any(d["missing"] == "page:ghost-page" for d in report.dangling_structural)


def test_clean_graph_reports_clean(wiki):
    store.write_page(Page(
        id="atlas", title="Atlas uses Redis", body="b",
        tags=["project:atlas", "library:redis"],
        relations=[{"s": "project:atlas", "p": "uses", "o": "library:redis"}],
    ))
    graph.rebuild_graph(now=NOW)
    report = graph_lint.graph_lint(fix=True, now=NOW)
    assert report.changes == 0 and report.flagged == 0
    assert "clean" in report.summary()
