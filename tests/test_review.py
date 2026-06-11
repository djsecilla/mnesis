"""Tests for the contradiction review queue: list and resolve (stub mode)."""

from __future__ import annotations

import subprocess

import pytest

from mnesis import config, confidence, ingest, mcp_server, state, store
from mnesis.store import Page

TITLE = "Project Atlas uses Redis for caching"


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


def _conf(page_id: str) -> float:
    page = store.read_page(page_id)
    return confidence.compute_confidence(page, access=state.get_access(page_id))[0]


def _queue_a_contradiction() -> tuple[Page, Page, int]:
    """Seed E, ingest a low-margin contradicting source, return (E, new, review_id)."""
    from mnesis import search

    existing = Page(id="atlas-cache", title=TITLE, body=f"{TITLE} as its layer.", source_count=1)
    store.write_page(existing)
    search.rebuild()
    new = ingest.ingest_source(
        f"{TITLE}. relation:contradicts It actually uses Postgres.", "src-2"
    )
    review_id = state.list_open_reviews()[0]["id"]
    return existing, new, review_id


def test_review_lists_open_contradiction(wiki):
    existing, new, review_id = _queue_a_contradiction()

    out = mcp_server.mnesis_review()
    assert f"#{review_id}" in out
    assert existing.id in out and new.id in out
    assert "conf" in out  # confidences shown
    assert "Postgres" in out or "conflicts" in out  # detail shown


def test_resolve_supersedes_loser_lifts_confidence_and_empties_queue(wiki):
    existing, new, review_id = _queue_a_contradiction()
    conf_before = _conf(existing.id)  # penalised by the contradiction

    result = mcp_server.mnesis_resolve(review_id, existing.id)
    assert result.startswith("resolved review")

    # Loser superseded -> stale, links both ways.
    loser = store.read_page(new.id)
    keeper = store.read_page(existing.id)
    assert loser.status == "stale"
    assert loser.superseded_by == existing.id
    assert keeper.supersedes == new.id

    # Mutual contradicts cleared on BOTH pages.
    assert new.id not in keeper.contradicts
    assert existing.id not in loser.contradicts

    # Kept page's confidence lifted (contradiction_factor penalty gone).
    assert _conf(existing.id) > conf_before

    # Queue empty, and the resolved review never returns.
    assert state.list_open_reviews() == []
    assert "(no open contradictions)" in mcp_server.mnesis_review()


def test_resolve_rejects_bad_inputs(wiki):
    _, _, review_id = _queue_a_contradiction()
    assert "no open review" in mcp_server.mnesis_resolve(9999, "atlas-cache")
    assert "not part of review" in mcp_server.mnesis_resolve(review_id, "some-other-page")
    # The queue is untouched by the failed attempts.
    assert len(state.list_open_reviews()) == 1


def test_query_notes_open_contradiction(wiki):
    _queue_a_contradiction()
    out = mcp_server.mnesis_query("redis caching", include_stale=True)
    assert "contradiction under review" in out


def test_resolved_review_does_not_return_after_decay(wiki):
    from mnesis import lifecycle

    existing, new, review_id = _queue_a_contradiction()
    mcp_server.mnesis_resolve(review_id, existing.id)
    assert state.list_open_reviews() == []

    # A later decay pass must not resurrect the resolved review.
    lifecycle.recompute_all()
    assert state.list_open_reviews() == []
    assert "(no open contradictions)" in mcp_server.mnesis_review()
    # The superseded loser remains as stale history (never deleted).
    assert store.read_page(new.id).status == "stale"
