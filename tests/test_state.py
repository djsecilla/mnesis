"""Tests for the durable state store (access events + review queue)."""

from __future__ import annotations

import pytest

from mnesis import config, state


@pytest.fixture()
def index(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "INDEX_DIR", tmp_path / ".index")
    return tmp_path


def test_record_access_increments_count(index):
    assert state.get_access("page-x") is None  # unseen
    state.record_access("page-x")
    state.record_access("page-x")
    acc = state.get_access("page-x")
    assert acc is not None
    assert acc["count"] == 2
    assert acc["last_accessed"]  # an ISO timestamp was recorded


def test_review_queue_enqueue_list_resolve(index):
    rid = state.enqueue_contradiction("page-a", "page-b", "claims conflict on cache TTL")
    assert isinstance(rid, int)

    open_reviews = state.list_open_reviews()
    assert len(open_reviews) == 1
    r = open_reviews[0]
    assert r["id"] == rid
    assert r["page_a"] == "page-a" and r["page_b"] == "page-b"
    assert r["kind"] == "contradiction"
    assert r["detail"] == "claims conflict on cache TTL"
    assert r["status"] == "open"

    state.resolve_review(rid)
    assert state.list_open_reviews() == []  # no longer open


def test_state_db_created_on_demand_and_separate_file(index):
    # Touching the state store creates state.db (not the search index).
    state.record_access("p")
    assert (config.INDEX_DIR / "state.db").exists()


def test_state_survives_search_rebuild(index, monkeypatch):
    # rebuild() must not touch the state store. Point pages at an empty dir.
    from mnesis import search

    monkeypatch.setattr(config, "PAGES_DIR", index / "pages")
    (index / "pages").mkdir(parents=True)

    state.record_access("durable-page")
    rid = state.enqueue_contradiction("a", "b", "x")

    search.rebuild()  # rebuilds wiki.db only

    assert state.get_access("durable-page")["count"] == 1
    assert any(r["id"] == rid for r in state.list_open_reviews())
