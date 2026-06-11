"""Tests for confidence-blended retrieval and access-driven reinforcement."""

from __future__ import annotations

import subprocess

import pytest

from mnesis import config, mcp_server, search, state, store
from mnesis.store import Page


@pytest.fixture()
def wiki(tmp_path, monkeypatch):
    root = tmp_path / "wiki"
    (root / "pages").mkdir(parents=True)
    (root / "sources").mkdir(parents=True)
    monkeypatch.setattr(config, "MNESIS_ROOT", root)
    monkeypatch.setattr(config, "PAGES_DIR", root / "pages")
    monkeypatch.setattr(config, "SOURCES_DIR", root / "sources")
    monkeypatch.setattr(config, "INDEX_DIR", root / ".index")
    monkeypatch.setattr(config, "MNESIS_LLM_STUB", True)

    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@localhost"], check=True
    )
    return tmp_path


_BODY = "Redis caching layer for the project."


def test_higher_confidence_ranks_first(wiki):
    # Identical bodies (so BM25 ties), different support -> confidence decides.
    store.write_page(Page(id="p-low", title="Low", body=_BODY, source_count=1))
    store.write_page(Page(id="p-high", title="High", body=_BODY, source_count=3))
    search.rebuild()

    hits = search.search("redis caching")
    assert [h.id for h in hits][:2] == ["p-high", "p-low"]
    assert hits[0].confidence > hits[1].confidence
    assert hits[0].final_score > hits[1].final_score


def test_stale_excluded_by_default_and_demoted_when_included(wiki):
    store.write_page(Page(id="act", title="Active", body=_BODY, source_count=3))
    store.write_page(Page(id="stl", title="Stale", body=_BODY, source_count=3, status="stale"))
    search.rebuild()

    default_ids = [h.id for h in search.search("redis")]
    assert default_ids == ["act"]  # stale excluded

    with_stale = [h.id for h in search.search("redis", include_stale=True)]
    assert set(with_stale) == {"act", "stl"}
    assert with_stale[0] == "act"  # stale demoted, never outranks the active page


def test_reading_increments_access_and_nudges_confidence(wiki):
    store.write_page(Page(id="p", title="P", body=_BODY, source_count=1))
    search.rebuild()
    before = search.search("redis")[0].confidence
    assert state.get_access("p") is None

    mcp_server.wiki_get("p")  # reinforcement on read

    assert state.get_access("p")["count"] == 1
    after = search.search("redis")[0].confidence
    assert after > before  # access boost applied to the cached confidence


def test_rebuild_preserves_access_and_reproduces_ranking(wiki):
    store.write_page(Page(id="p-low", title="Low", body=_BODY, source_count=1))
    store.write_page(Page(id="p-high", title="High", body=_BODY, source_count=3))
    search.rebuild()

    mcp_server.wiki_get("p-low")  # build up some durable access state
    mcp_server.wiki_get("p-low")
    order_before = [h.id for h in search.search("redis caching", include_stale=True)]

    # Blow away the search index; the state store must survive.
    (config.INDEX_DIR / "wiki.db").unlink()
    search.rebuild()

    assert state.get_access("p-low")["count"] == 2  # access state preserved
    order_after = [h.id for h in search.search("redis caching", include_stale=True)]
    assert order_after == order_before  # ranking reproduced
