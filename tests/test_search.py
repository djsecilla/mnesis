"""Tests for the FTS5 keyword index, including the rebuildable-cache invariant."""

from __future__ import annotations

import subprocess

import pytest

from mnesis import config, search, store, tenancy
from mnesis.store import Page


@pytest.fixture()
def wiki(tenant):
    return tenant.root_path

def _seed_three_pages():
    store.write_page(
        Page(
            id="atlas-redis-cache",
            title="Project Atlas uses Redis for caching",
            body="Project Atlas uses Redis as its primary caching layer for hot data.",
            tags=["project:atlas", "library:redis", "concept:caching"],
        )
    )
    store.write_page(
        Page(
            id="billing-postgres",
            title="Billing service uses PostgreSQL",
            body="The billing service stores invoices in a PostgreSQL database.",
            tags=["project:billing", "library:postgresql"],
        )
    )
    store.write_page(
        Page(
            id="sarah-auth-migration",
            title="Sarah owns the auth migration",
            body="Sarah leads the authentication migration workstream.",
            tags=["person:sarah", "decision:auth-migration"],
        )
    )


def test_rebuild_and_search_top_hit(wiki):
    _seed_three_pages()
    assert search.rebuild() == 3

    hits = search.search("redis caching")
    assert hits, "expected at least one hit"
    assert hits[0].id == "atlas-redis-cache"


def test_natural_language_question_retrieves_pages(wiki):
    """Questions must not require every word to appear (OR + prefix, stopwords
    dropped) — otherwise chat/search would say "nothing in the wiki" for real
    questions. Regression for the implicit-AND retrieval bug."""
    _seed_three_pages()
    search.rebuild()

    # A full question whose function words appear on no page still finds Atlas.
    hits = search.search("What does Project Atlas use for caching?")
    assert hits and hits[0].id == "atlas-redis-cache"

    # Multi-term query where not every term is present still recalls the page,
    # and a morphological variant (postgres -> postgresql) matches via prefix.
    hits = search.search("billing service postgres")
    assert any(h.id == "billing-postgres" for h in hits)
    assert hits[0].snippet  # non-empty snippet


def test_index_is_rebuildable_identically(wiki):
    _seed_three_pages()
    search.rebuild()
    before = [(h.id, h.bm25_score, h.snippet) for h in search.search("redis caching")]

    # Blow away the cache entirely, then rebuild from Markdown alone.
    (tenancy.current().cache_dir / "wiki.db").unlink()
    count = search.rebuild()
    assert count == 3
    after = [(h.id, h.bm25_score, h.snippet) for h in search.search("redis caching")]

    assert before == after  # identical: id, BM25 score, and snippet


def test_search_empty_query_returns_nothing(wiki):
    _seed_three_pages()
    search.rebuild()
    assert search.search("") == []
    assert search.search("   !?@  ") == []


def test_upsert_indexes_single_page(wiki):
    search.rebuild()  # empty index
    assert search.search("postgresql") == []

    page = Page(
        id="billing-postgres",
        title="Billing service uses PostgreSQL",
        body="The billing service stores invoices in a PostgreSQL database.",
        tags=["library:postgresql"],
    )
    store.write_page(page)
    search.upsert(page)

    hits = search.search("postgresql")
    assert [h.id for h in hits] == ["billing-postgres"]

    # Upserting again must not duplicate the row.
    search.upsert(page)
    assert len(search.search("postgresql")) == 1
