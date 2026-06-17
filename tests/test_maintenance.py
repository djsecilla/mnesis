"""Tests for the read-only maintenance MCP tools (health report, duplicate
finder) plus parity of the graph-lint tool with the CLI/module."""

from __future__ import annotations

import subprocess

import pytest

from mnesis import config, graph, graph_lint, maintenance, mcp_server, search, state, store
from mnesis.store import Page


def _git(tmp_path, *args):
    return subprocess.run(
        ["git", "-C", str(tmp_path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture()
def wiki(tmp_path, monkeypatch):
    root = tmp_path / "wiki"
    (root / "pages").mkdir(parents=True)
    monkeypatch.setattr(config, "MNESIS_ROOT", root)
    monkeypatch.setattr(config, "PAGES_DIR", root / "pages")
    monkeypatch.setattr(config, "SOURCES_DIR", root / "sources")
    monkeypatch.setattr(config, "INDEX_DIR", root / ".index")
    monkeypatch.setattr(config, "GRAPH_BACKEND", "sqlite")
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@localhost"], check=True
    )

    # A planted near-duplicate pair: near-identical titles, same tags, same edge.
    store.write_page(Page(
        id="atlas-redis", title="Project Atlas uses Redis for caching",
        body="Project Atlas uses Redis as its cache.",
        sources=["atlas-notes"], tags=["project:atlas", "library:redis"],
        relations=[{"s": "project:atlas", "p": "uses", "o": "library:redis"}],
    ))
    store.write_page(Page(
        id="atlas-redis-dup", title="Project Atlas uses Redis for the caching layer",
        body="Atlas relies on Redis for caching.",
        sources=["atlas-arch"], tags=["project:atlas", "library:redis"],
        relations=[{"s": "project:atlas", "p": "uses", "o": "library:redis"}],
    ))
    # A clearly unrelated page (should not pair with the Atlas ones).
    store.write_page(Page(
        id="pg-backups", title="Postgres backups run nightly",
        body="Nightly backups of Postgres.",
        sources=["ops-runbook"], tags=["library:postgres"],
    ))
    # A page with no sources at all (health: no_sources).
    store.write_page(Page(
        id="orphan-note", title="A loose observation about scaling",
        body="Some scaling thought.", sources=[], kind="note",
    ))

    search.rebuild()
    graph.rebuild_graph()
    return tmp_path


# --- health report ----------------------------------------------------------


def test_health_report_documented_shape(wiki):
    r = maintenance.health_report()
    assert r["pages_total"] == 4
    assert r["by_status"]["active"] == 4
    assert r["by_kind"]["fact"] == 3 and r["by_kind"]["note"] == 1
    assert r["no_sources"] == ["orphan-note"]
    assert isinstance(r["low_confidence"], int)
    assert r["stale"] == 0
    assert r["open_contradictions"] == 0
    assert r["graph"]["entities"] > 0 and r["graph"]["edges"] > 0
    for key in ("orphan_entities", "undeclared_entities", "dangling_structural"):
        assert isinstance(r[key], int)
    # Caches fresh right after a rebuild.
    assert r["index"]["markdown_pages"] == 4 and r["index"]["indexed_pages"] == 4
    assert r["index"]["fresh"] is True
    assert r["graph_index"]["present"] is True and r["graph_index"]["fresh"] is True


def test_health_report_detects_stale_index(wiki):
    # Add a page on disk only (no upsert) — index drifts from Markdown.
    store.write_page(Page(id="late", title="Added after rebuild", sources=["x"]))
    r = maintenance.health_report()
    assert r["index"]["fresh"] is False
    assert "late" in r["index"]["missing_from_index"]
    assert r["graph_index"]["fresh"] is False
    assert "late" in r["graph_index"]["missing_page_nodes"]


def test_mnesis_health_report_text(wiki):
    out = mcp_server.mnesis_health_report()
    assert "pages: 4" in out
    assert "open contradictions: 0" in out
    assert "search index: 4/4 pages, fresh" in out
    assert "graph cache: present, fresh" in out


# --- duplicate finder -------------------------------------------------------


def test_find_duplicates_surfaces_planted_pair(wiki):
    dupes = maintenance.find_duplicates()
    pairs = {(d["page_a"], d["page_b"]) for d in dupes}
    assert ("atlas-redis", "atlas-redis-dup") in pairs
    top = dupes[0]
    assert {top["page_a"], top["page_b"]} == {"atlas-redis", "atlas-redis-dup"}
    assert top["similarity"] > 0.25
    assert top["signals"]["edges"] == 1.0  # identical relation
    assert "shared tags" in top["rationale"]
    # The unrelated page never pairs with the Atlas cluster.
    assert ("atlas-redis", "pg-backups") not in pairs
    assert ("atlas-redis-dup", "pg-backups") not in pairs


def test_find_duplicates_excludes_supersede_pairs(wiki):
    # Supersede the dup; the now-linked pair must not be flagged as a duplicate.
    new = store.read_page("atlas-redis-dup")
    store.supersede("atlas-redis", new)
    search.rebuild()
    graph.rebuild_graph()
    pairs = {(d["page_a"], d["page_b"]) for d in maintenance.find_duplicates()}
    assert ("atlas-redis", "atlas-redis-dup") not in pairs


def test_mnesis_find_duplicates_text(wiki):
    out = mcp_server.mnesis_find_duplicates()
    assert "near-duplicate candidates" in out
    assert "atlas-redis" in out and "atlas-redis-dup" in out
    assert "Phase-5 vectors" in out


# --- read tools are side-effect-free ----------------------------------------


def test_read_tools_write_nothing(wiki):
    before_head = _git(wiki, "rev-parse", "HEAD")
    before_status = _git(wiki, "status", "--porcelain")
    before_state = state.list_open_reviews()

    maintenance.health_report()
    maintenance.find_duplicates()
    mcp_server.mnesis_health_report()
    mcp_server.mnesis_find_duplicates()

    assert _git(wiki, "rev-parse", "HEAD") == before_head
    assert _git(wiki, "status", "--porcelain") == before_status
    assert state.list_open_reviews() == before_state


# --- graph-lint parity ------------------------------------------------------


def test_graph_lint_mcp_matches_module(wiki):
    assert mcp_server.mnesis_graph_lint(False) == graph_lint.graph_lint(fix=False).summary()
