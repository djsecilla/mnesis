"""Relation-aware ingest (Phase 2): reinforce / supersede / contradict / create.

Stub mode drives each branch deterministically via a ``relation:<label>`` marker
embedded in the new source text.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from mnesis import config, confidence, ingest, search, state, store
from mnesis.store import Page

TITLE = "Project Atlas uses Redis for caching"


@pytest.fixture()
def wiki(tenant):
    return tenant.root_path

def _seed_existing(days_old: int = 0, source_count: int = 1) -> Page:
    """Create the existing page E and index it so ingest can find it as a candidate."""
    lc = (datetime.now(timezone.utc) - timedelta(days=days_old)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    page = Page(
        id="atlas-cache",
        title=TITLE,
        body=f"{TITLE} as its primary layer.",
        sources=["seed-source"],
        source_count=source_count,
        last_confirmed=lc,
        kind="fact",
    )
    store.write_page(page)
    search.rebuild()
    return page


def _conf(page: Page) -> float:
    return confidence.compute_confidence(page, access=state.get_access(page.id))[0]


def test_reinforces_bumps_support_without_new_page(wiki):
    existing = _seed_existing()
    before = _conf(existing)

    result = ingest.ingest_source(f"{TITLE}. relation:reinforces", "src-2")

    assert result.id == existing.id  # same page, not a new one
    assert len(store.list_pages()) == 1
    reread = store.read_page(existing.id)
    assert reread.source_count == 2
    assert "src-2" in reread.sources
    assert _conf(reread) > before  # confidence bumped


def test_supersedes_creates_new_and_stales_old(wiki):
    existing = _seed_existing()

    new = ingest.ingest_source(f"{TITLE}. relation:supersedes Now it uses Memcached.", "src-2")

    assert new.id != existing.id
    assert new.status == "active"
    assert new.supersedes == existing.id
    old = store.read_page(existing.id)
    assert old.status == "stale"
    assert old.superseded_by == new.id


def test_low_margin_contradiction_coexists_and_queues(wiki):
    existing = _seed_existing()  # fresh -> comparable confidence to the new page

    new = ingest.ingest_source(
        f"{TITLE}. relation:contradicts It actually uses Postgres for caching.", "src-2"
    )

    # Both pages live, cross-linked, with a queued review.
    assert new.status == "active"
    assert existing.id in new.contradicts
    old = store.read_page(existing.id)
    assert new.id in old.contradicts
    assert old.status == "active"
    assert len(store.list_pages(kind="fact")) == 2
    open_reviews = state.list_open_reviews()
    assert len(open_reviews) == 1
    assert {open_reviews[0]["page_a"], open_reviews[0]["page_b"]} == {new.id, existing.id}


def test_clear_margin_contradiction_auto_supersedes(wiki):
    # Aged, low-confidence existing page loses to a fresh contradicting source.
    existing = _seed_existing(days_old=200)
    assert _conf(existing) < 0.5  # clearly weaker

    new = ingest.ingest_source(
        f"{TITLE}. relation:contradicts It now uses Postgres.", "src-2"
    )

    assert new.supersedes == existing.id
    old = store.read_page(existing.id)
    assert old.status == "stale"
    assert old.superseded_by == new.id
    assert state.list_open_reviews() == []  # auto-resolved, nothing queued


def test_unrelated_creates_fresh_page(wiki):
    _seed_existing()

    new = ingest.ingest_source(f"{TITLE}. relation:unrelated", "src-2")

    assert new.id != "atlas-cache"
    assert len(store.list_pages()) == 2  # candidate found but classified unrelated -> new page
