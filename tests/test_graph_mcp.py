"""Tests for the graph MCP/CLI tool functions (called directly, stub mode)."""

from __future__ import annotations

import subprocess

import pytest

from mnesis import config, graph, mcp_server, search, store
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
    store.write_page(Page(
        id="atlas", title="Atlas uses Redis", body="Project Atlas uses Redis.",
        tags=["project:atlas", "library:redis"],
        relations=[{"s": "project:atlas", "p": "uses", "o": "library:redis"}],
    ))
    store.write_page(Page(
        id="auth", title="Auth migration depends on Redis", body="Owned by Sarah.",
        tags=["decision:auth-migration", "person:sarah"],
        relations=[{"s": "decision:auth-migration", "p": "depends_on", "o": "library:redis"}],
    ))
    search.rebuild()
    graph.rebuild_graph()
    return tmp_path


def test_mnesis_entity_shows_type_pages_and_edges(wiki):
    out = mcp_server.mnesis_entity("library:redis")
    assert "type: library" in out
    # Both incident edges shown, each citing its grounding page.
    assert "project:atlas -uses-> library:redis" in out
    assert "decision:auth-migration -depends_on-> library:redis" in out
    assert "pages: atlas" in out and "pages: auth" in out
    assert "conf 0." in out  # confidence shown
    assert mcp_server.mnesis_entity("library:nonexistent") == "no such entity: library:nonexistent"


def test_mnesis_neighbors_directions_and_filter(wiki):
    inc = mcp_server.mnesis_neighbors("library:redis", direction="in")
    assert "project:atlas" in inc and "decision:auth-migration" in inc
    assert "pages:" in inc  # grounded

    # Outgoing from redis: none.
    assert mcp_server.mnesis_neighbors("library:redis", direction="out") == "no neighbors for library:redis"
    # Predicate filter.
    filtered = mcp_server.mnesis_neighbors("library:redis", predicate="uses", direction="in")
    assert "project:atlas" in filtered and "decision:auth-migration" not in filtered


def test_mnesis_traverse_paths(wiki):
    out = mcp_server.mnesis_traverse("project:atlas", depth=2)
    assert "project:atlas -> library:redis" in out


def test_mnesis_impact_paths_and_grounding(wiki):
    out = mcp_server.mnesis_impact("library:redis")
    assert "decision:auth-migration" in out
    assert "decision:auth-migration -> library:redis" in out
    assert "auth" in out  # grounding page cited


def test_mnesis_graph_stats_counts_by_type(wiki):
    out = mcp_server.mnesis_graph_stats()
    assert "entities:" in out and "edges:" in out and "demoted:" in out
    assert "by entity type:" in out
    assert "library=1" in out  # one library entity
    assert "by predicate:" in out


def test_query_and_get_note_related_entities(wiki):
    q = mcp_server.mnesis_query("redis")
    assert "related entities:" in q
    assert "library:redis" in q

    g = mcp_server.mnesis_get("atlas")
    assert "related entities:" in g
    assert "project:atlas" in g
