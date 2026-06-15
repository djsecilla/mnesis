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
        # engineering / project relations
        "uses", "depends_on", "owns", "caused", "fixed", "contradicts", "supersedes",
        # general-purpose relations
        "part_of", "located_in", "created", "precedes", "influences", "related_to",
    }


def test_general_purpose_predicates_validate():
    # A general-knowledge relation now forms a valid edge instead of being dropped.
    rel = vocab.validate_relation({"s": "project:atlas", "p": "part_of", "o": "project:helios"})
    assert rel == {"s": "project:atlas", "p": "part_of", "o": "project:helios"}
    assert vocab.is_valid_predicate("related_to")


def test_predicate_matching_is_normalized():
    # "Depends On" / "depends-on" / "depends_on" all resolve to the same predicate.
    for variant in ("Depends On", "depends-on", "DEPENDS_ON"):
        rel = vocab.validate_relation({"s": "project:atlas", "p": variant, "o": "library:redis"})
        assert rel["p"] == "depends_on"


def test_custom_predicates_from_config(monkeypatch):
    # MNESIS_PREDICATES replaces the default set; entries are snake_cased and the
    # structural predicates are always present.
    monkeypatch.setattr(config, "MNESIS_PREDICATES", "uses, Part Of, located-in")
    resolved = vocab._resolve_predicates()
    assert "uses" in resolved and "part_of" in resolved and "located_in" in resolved
    assert "supersedes" in resolved and "contradicts" in resolved  # core, always forced
    assert "owns" not in resolved  # not in the custom list -> excluded


def test_empty_config_uses_default(monkeypatch):
    monkeypatch.setattr(config, "MNESIS_PREDICATES", "")
    assert vocab._resolve_predicates() == vocab.PREDICATES


def test_custom_entity_types_from_config(monkeypatch):
    # MNESIS_ENTITY_TYPES replaces the default set; entries are snake_cased, the
    # reserved "page" type is dropped, and there is no forced core.
    monkeypatch.setattr(config, "MNESIS_ENTITY_TYPES", "Person, org, place, page, code file")
    resolved = vocab._resolve_entity_types()
    assert resolved == ("person", "org", "place", "code_file")  # order preserved, page dropped
    assert "project" not in resolved  # default excluded under a custom list


def test_empty_entity_type_config_uses_default(monkeypatch):
    monkeypatch.setattr(config, "MNESIS_ENTITY_TYPES", "")
    assert vocab._resolve_entity_types() == vocab.DEFAULT_ENTITY_TYPES


def test_symmetric_predicates_default_and_canonicalization():
    # contradicts/related_to are symmetric by default; directed ones are not.
    assert vocab.is_symmetric("related_to") and vocab.is_symmetric("contradicts")
    assert not vocab.is_symmetric("uses") and not vocab.is_symmetric("depends_on")
    # Symmetric edges canonicalise endpoint order so reciprocals collapse.
    assert vocab.canonical_edge("concept:b", "related_to", "concept:a") == (
        "concept:a", "related_to", "concept:b"
    )
    assert vocab.canonical_edge("concept:a", "related_to", "concept:b") == (
        "concept:a", "related_to", "concept:b"
    )
    # Directed edges keep their order.
    assert vocab.canonical_edge("project:b", "uses", "project:a") == ("project:b", "uses", "project:a")


def test_symmetric_config_override_and_intersection(monkeypatch):
    # Replaces the default; intersected with the active predicate set (a symmetric
    # predicate that isn't a valid predicate is dropped).
    monkeypatch.setattr(config, "MNESIS_SYMMETRIC_PREDICATES", "related_to, not_a_predicate")
    assert vocab._resolve_symmetric() == frozenset({"related_to"})
    # Empty disables symmetric handling entirely.
    monkeypatch.setattr(config, "MNESIS_SYMMETRIC_PREDICATES", "")
    assert vocab._resolve_symmetric() == frozenset()


def test_entity_type_matching_is_normalized(monkeypatch):
    # A ref's type is snake_cased the same way the vocabulary is, so a custom
    # multi-word type round-trips.
    monkeypatch.setattr(vocab, "ENTITY_TYPES", ("person", "code_file"))
    assert vocab.normalize_ref("Code File:auth-utils") == "code_file:auth-utils"


@pytest.fixture()
def wiki(tmp_path, monkeypatch):
    root = tmp_path / "wiki"
    (root / "pages").mkdir(parents=True)
    monkeypatch.setattr(config, "MNESIS_ROOT", root)
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
