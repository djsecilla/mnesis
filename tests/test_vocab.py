"""Tests for the Phase-3 entity/predicate vocabulary and relations round-trip."""

from __future__ import annotations

import subprocess

import pytest

from mnesis import config, store, vocab
from mnesis.store import Page


def test_valid_triple_normalizes():
    rel = vocab.validate_relation({"s": "project:atlas", "p": "uses", "o": "library:redis"})
    assert rel == {"s": "project:atlas", "p": "uses", "o": "library:redis"}


def test_mixed_case_and_spaced_refs_normalize_deterministically():
    a = vocab.normalize_ref("Project: Atlas Core")
    b = vocab.normalize_ref("project:atlas-core")
    assert a == b == "project:atlas-core"
    # Predicate case-insensitive; refs normalized inside a relation too.
    rel = vocab.validate_relation({"s": "Person:Sarah Lee", "p": "OWNS", "o": "decision:Auth Migration"})
    assert rel == {"s": "person:sarah-lee", "p": "owns", "o": "decision:auth-migration"}


def test_unknown_predicate_rejected_with_clear_error():
    with pytest.raises(ValueError, match="unknown predicate"):
        vocab.validate_relation({"s": "project:atlas", "p": "frobnicates", "o": "library:redis"})


def test_ref_without_valid_type_prefix_rejected():
    with pytest.raises(ValueError, match="unknown entity type"):
        vocab.normalize_ref("widget:thing")
    with pytest.raises(ValueError, match="must be 'type:value'"):
        vocab.normalize_ref("no-colon-here")
    with pytest.raises(ValueError, match="empty value"):
        vocab.normalize_ref("project:   ")


def test_validate_relation_rejects_bad_shapes():
    with pytest.raises(ValueError, match="missing key"):
        vocab.validate_relation({"s": "project:atlas", "p": "uses"})
    with pytest.raises(ValueError, match="mapping"):
        vocab.validate_relation(["project:atlas", "uses", "library:redis"])


def test_predicate_and_type_constants_match_contract():
    assert set(vocab.ENTITY_TYPES) == {"person", "project", "library", "concept", "file", "decision"}
    assert set(vocab.PREDICATES) == {
        "uses", "depends_on", "owns", "caused", "fixed", "contradicts", "supersedes"
    }


@pytest.fixture()
def wiki(tmp_path, monkeypatch):
    root = tmp_path / "wiki"
    (root / "pages").mkdir(parents=True)
    monkeypatch.setattr(config, "WIKI_ROOT", root)
    monkeypatch.setattr(config, "PAGES_DIR", root / "pages")
    monkeypatch.setattr(config, "INDEX_DIR", root / ".index")
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@localhost"], check=True
    )
    return tmp_path


def test_page_roundtrips_with_relations(wiki):
    relations = [
        {"s": "project:atlas", "p": "uses", "o": "library:redis"},
        {"s": "person:sarah", "p": "owns", "o": "decision:auth-migration"},
    ]
    page = Page(
        id="atlas-redis",
        title="Project Atlas uses Redis for caching",
        body="Atlas uses Redis.",
        tags=["project:atlas", "library:redis"],
        relations=relations,
    )
    store.write_page(page)

    reread = store.read_page("atlas-redis")
    assert reread.relations == relations
    assert reread == page  # full round-trip identical


def test_phase1_page_reads_with_empty_relations(wiki):
    # A page written without a relations field still parses (backward compatible).
    path = wiki / "wiki" / "pages" / "legacy.md"
    path.write_text(
        "---\n"
        "id: legacy\n"
        "title: A legacy page\n"
        "created: '2026-01-01T00:00:00.000000Z'\n"
        "updated: '2026-01-01T00:00:00.000000Z'\n"
        "sources: [s1]\n"
        "source_count: 1\n"
        "last_confirmed: '2026-01-01T00:00:00.000000Z'\n"
        "tags: []\n"
        "kind: fact\n"
        "status: active\n"
        "supersedes: null\n"
        "superseded_by: null\n"
        "---\n"
        "Body.\n",
        encoding="utf-8",
    )
    page = store.read_page("legacy")
    assert page.relations == []
