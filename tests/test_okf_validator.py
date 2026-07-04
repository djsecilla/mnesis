"""OKF1 — Open Knowledge Format v0.1 conformance contract + validator.

Grounded in the spec (okf/SPEC.md): `type` is the one required field; the recommended
fields (title/description/resource/tags/timestamp) are optional; consumers must tolerate
unknown types/keys, broken links, and missing optional fields; reserved files (index.md
with no frontmatter, log.md with ISO-date headings) have a strict structure. Mnesis
extension fields ride along as tolerated extra keys and never break conformance.
"""

from __future__ import annotations

from mnesis import okf
from mnesis.store import Page

# A document shaped like the OKF reference sample: type + the reference-parser fields,
# a body with a bundle-absolute cross-link (the relationship kind is in the prose).
REFERENCE_SAMPLE = """---
type: table
title: Users
description: The primary users table.
tags: [database, pii]
timestamp: 2026-06-10T17:25:20Z
---
The users table stores account records. It is read by
[the auth service](/services/auth), which depends on it.
"""


# ── conformant documents validate ──────────────────────────────────────────


def test_handwritten_conformant_document_validates():
    doc = """---
type: fact
title: Project Atlas uses Redis for caching
description: Atlas uses Redis as its primary cache.
tags: [project:atlas, library:redis]
timestamp: 2026-06-09T10:15:00Z
---
Project Atlas uses Redis. The [auth migration](/auth-migration) depends on it.
"""
    r = okf.validate_document(doc, path="project-atlas-redis-cache.md")
    assert r.conformant and not r.warnings


def test_reference_sample_shape_validates():
    r = okf.validate_document(REFERENCE_SAMPLE, path="tables/users.md")
    assert r.conformant and not r.errors
    # OKF identity is the bundle-relative path minus .md.
    assert okf.concept_id("tables/users.md") == "tables/users"
    # The cross-link is discoverable (relationship; kind conveyed by prose).
    assert "/services/auth" in okf.cross_links(REFERENCE_SAMPLE)


# ── the one required field ──────────────────────────────────────────────────


def test_missing_type_fails():
    r = okf.validate_document("---\ntitle: X\ndescription: y\n---\nbody\n", path="x.md")
    assert not r.conformant
    assert [i.code for i in r.errors] == ["missing_type"]


def test_empty_type_fails():
    r = okf.validate_document("---\ntype: '   '\n---\nb\n", path="x.md")
    assert not r.conformant and any(i.code == "missing_type" for i in r.errors)


def test_no_frontmatter_fails():
    r = okf.validate_document("just prose, no frontmatter\n", path="x.md")
    assert not r.conformant and any(i.code == "no_frontmatter" for i in r.errors)


# ── malformed frontmatter is flagged ────────────────────────────────────────


def test_malformed_frontmatter_is_flagged():
    bad = "---\ntype: fact\ntags: [a, b\ntitle: \"unterminated\n---\nbody\n"
    r = okf.validate_document(bad, path="bad.md")
    assert not r.conformant
    assert any(i.code == "unparseable_frontmatter" for i in r.errors)


# ── OKF leniency: unknown type/keys, broken links, missing optionals ────────


def test_unknown_type_and_extra_keys_are_tolerated():
    doc = """---
type: some-brand-new-concept-kind
title: T
description: d
timestamp: 2026-01-01T00:00:00Z
mnesis_ext_a: 1
kind: fact
relations: [{s: project:x, p: uses, o: library:y}]
---
A link to [something missing](/does-not-exist) — a broken link is tolerated.
"""
    r = okf.validate_document(doc, path="t.md")
    assert r.conformant  # unknown type, extra keys, and a broken link are all fine


def test_missing_recommended_fields_are_warnings_not_errors():
    r = okf.validate_document("---\ntype: fact\n---\nbody\n", path="x.md")
    assert r.conformant  # still conforms (type present)
    warned = {i.field for i in r.warnings if i.code == "missing_recommended"}
    assert {"title", "description", "timestamp"} <= warned


def test_non_iso_timestamp_is_a_warning():
    r = okf.validate_document("---\ntype: fact\ntitle: T\ndescription: d\ntimestamp: yesterday\n---\nb\n",
                              path="x.md")
    assert r.conformant and any(i.code == "timestamp_not_iso8601" for i in r.warnings)


# ── reserved files ──────────────────────────────────────────────────────────


def test_reserved_index_must_not_have_frontmatter():
    assert okf.validate_document("# Index\n- [a](/a)\n- [b](/b)\n", path="index.md").conformant
    bad = okf.validate_document("---\ntype: x\n---\n# Index\n", path="index.md")
    assert not bad.conformant and any(i.code == "index_has_frontmatter" for i in bad.errors)


def test_reserved_log_iso_date_headings():
    ok = "# Changelog\n\n## 2026-06-10\nAdded the users table.\n"
    assert okf.validate_document(ok, path="log.md").conformant
    warn = okf.validate_document("# Changelog\n\n## Recently\nstuff\n", path="log.md")
    assert warn.conformant and any(i.code == "log_headings_not_iso" for i in warn.warnings)


# ── bundle validation ───────────────────────────────────────────────────────


def test_validate_bundle(tmp_path):
    (tmp_path / "a.md").write_text("---\ntype: fact\ntitle: A\ndescription: d\ntimestamp: 2026-01-01T00:00:00Z\n---\nlink to [b](/b)\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("---\ntype: note\ntitle: B\ndescription: d\ntimestamp: 2026-01-01T00:00:00Z\n---\nbody\n", encoding="utf-8")
    (tmp_path / "index.md").write_text("# Bundle\n- [a](/a)\n- [b](/b)\n", encoding="utf-8")  # no frontmatter: ok
    r = okf.validate_bundle(tmp_path)
    assert r.conformant and r.documents == 2  # index.md is reserved, not a concept doc

    # One malformed doc makes the whole bundle non-conformant, precisely located.
    (tmp_path / "broken.md").write_text("no frontmatter here\n", encoding="utf-8")
    r2 = okf.validate_bundle(tmp_path)
    assert not r2.conformant
    assert any(i.path == "broken.md" and i.code == "no_frontmatter" for i in r2.errors)


# ── Mnesis extensions do not break OKF conformance ──────────────────────────


def test_mnesis_page_maps_to_conformant_okf():
    page = Page(
        id="project-atlas-redis-cache",
        title="Project Atlas uses Redis for caching",
        body="Project Atlas uses Redis as its primary cache.\n\nSource: atlas-notes.",
        tags=["project:atlas", "library:redis"],
        relations=[{"s": "project:atlas", "p": "uses", "o": "library:redis"}],
        kind="fact",
    )
    doc = okf.to_okf_document(page)
    r = okf.validate_document(doc, path="project-atlas-redis-cache.md")
    assert r.conformant and not r.warnings  # every OKF-core field derived; extensions tolerated

    meta = okf.to_okf_metadata(page)
    assert meta["type"] == "fact"                       # OKF core ← kind
    assert meta["timestamp"] == page.updated            # OKF core ← updated
    assert meta["description"]                           # derived, non-empty
    assert meta["id"] == "project-atlas-redis-cache"    # extension alias of the path identity
    assert meta["relations"] == page.relations          # extension preserved verbatim
