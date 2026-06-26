"""Tests for Phase-3 entity & relation extraction on ingest (stub mode).

Entities/relations are driven by ``tag{...}`` / ``rel{s|p|o}`` markers in the
source text, which the offline stub turns into extracted tags and triples.
"""

from __future__ import annotations

import logging
import subprocess

import pytest

from mnesis import config, ingest, store


@pytest.fixture()
def wiki(tenant):
    return tenant.root_path

# A source asserting three valid relations (one ref intentionally mixed-case) plus
# one triple with an invalid predicate that must be dropped.
SOURCE = (
    "Project Atlas uses Redis for caching. "
    "The auth migration depends on Redis, and Sarah owns the auth migration. "
    "tag{Project:Atlas} tag{library:redis} tag{decision:auth-migration} tag{person:sarah} "
    "rel{project:atlas|uses|library:redis} "
    "rel{decision:auth-migration|depends_on|library:redis} "
    "rel{person:sarah|owns|decision:auth-migration} "
    "rel{project:atlas|frobnicates|library:redis}"
)


def test_extracts_normalized_tags_and_valid_relations(wiki):
    page = ingest.ingest_source(SOURCE, "atlas-arch")

    # Three valid, normalized triples; the invalid one is absent.
    assert page.relations == [
        {"s": "project:atlas", "p": "uses", "o": "library:redis"},
        {"s": "decision:auth-migration", "p": "depends_on", "o": "library:redis"},
        {"s": "person:sarah", "p": "owns", "o": "decision:auth-migration"},
    ]
    assert all(r["p"] != "frobnicates" for r in page.relations)

    # Tags are normalized entity refs (mixed-case "Project:Atlas" -> "project:atlas").
    assert "project:atlas" in page.tags
    assert {"library:redis", "decision:auth-migration", "person:sarah"} <= set(page.tags)

    # It round-trips on disk.
    assert store.read_page(page.id).relations == page.relations


def test_invalid_triple_is_dropped_and_reported(wiki, caplog):
    with caplog.at_level(logging.WARNING, logger="mnesis.ingest"):
        page = ingest.ingest_source(SOURCE, "atlas-arch")

    assert "dropped invalid relation" in caplog.text
    assert "frobnicates" in caplog.text
    assert len(page.relations) == 3  # only the valid ones written


def test_reinforcement_unions_new_relation_without_duplication(wiki):
    a = ingest.ingest_source(
        "Project Atlas uses Redis for caching. "
        "tag{project:atlas} tag{library:redis} rel{project:atlas|uses|library:redis}",
        "atlas-arch",
    )
    assert a.relations == [{"s": "project:atlas", "p": "uses", "o": "library:redis"}]

    # A second, agreeing source reinforces A and adds a new relation (plus a dup).
    reinforced = ingest.ingest_source(
        "Project Atlas uses Redis for caching. relation:reinforces "
        "tag{person:sarah} "
        "rel{project:atlas|uses|library:redis} "  # duplicate -> must not double
        "rel{person:sarah|owns|decision:auth-migration}",  # new -> unioned in
        "atlas-confirm",
    )

    assert reinforced.id == a.id  # reinforced, not a new page
    assert reinforced.source_count == 2
    assert reinforced.relations == [
        {"s": "project:atlas", "p": "uses", "o": "library:redis"},
        {"s": "person:sarah", "p": "owns", "o": "decision:auth-migration"},
    ]
    assert "person:sarah" in reinforced.tags
