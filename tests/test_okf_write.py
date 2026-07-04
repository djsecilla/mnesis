"""OKF2 — the OKF-conformant store write path.

Every newly written page is an OKF-conformant concept document (validated before commit),
its body carries OKF cross-links generated from its relations + lifecycle links, it is
addressable by its bundle path, and the reserved files index.md / log.md are generated
conformantly — all while preserving the existing write/commit/redaction behaviour.
"""

from __future__ import annotations

import subprocess

import pytest

from mnesis import okf, store, tenancy
from mnesis.store import Page


def _read(ctx, name: str) -> str:
    return (ctx.pages_dir / name).read_text(encoding="utf-8")


def _commit_count(ctx) -> int:
    out = subprocess.run(["git", "-C", str(ctx.root_path), "rev-list", "--count", "HEAD"],
                         capture_output=True, text=True)
    return int((out.stdout or "0").strip() or "0")


# ── a written page is OKF-conformant + validator-clean ─────────────────────


def test_written_page_validates_as_okf(tenant):
    store.write_page(Page(
        id="atlas", title="Project Atlas uses Redis for caching",
        body="Project Atlas uses Redis as its primary caching layer.",
        tags=["project:atlas", "library:redis"],
    ))
    raw = _read(tenant, "atlas.md")
    report = okf.validate_document(raw, path="atlas.md")
    assert report.conformant and not report.errors
    # OKF-core fields present on disk (type ← kind, timestamp ← updated), extensions kept.
    assert "type: fact" in raw and "timestamp:" in raw and "description:" in raw
    assert "kind: fact" in raw and "relations:" in raw  # tolerated Mnesis extensions


# ── body carries OKF cross-links matching the relations ────────────────────


def test_body_cross_links_match_relations(tenant):
    store.write_page(Page(
        id="atlas", title="Atlas uses Redis",
        body="Atlas uses Redis as its cache.",
        tags=["project:atlas", "library:redis"],
        relations=[{"s": "project:atlas", "p": "uses", "o": "library:redis"}],
    ))
    raw = _read(tenant, "atlas.md")
    links = okf.cross_links(raw)
    # Bundle-absolute Markdown links for both relation endpoints (the predicate is prose).
    assert "/project/atlas" in links and "/library/redis" in links
    assert "*uses*" in raw  # the relationship kind is conveyed by prose, not the link


def test_lifecycle_links_are_cross_linked(tenant):
    store.write_page(Page(id="old-atlas", title="Old Atlas fact", body="Old."))
    store.supersede("old-atlas", Page(id="new-atlas", title="New Atlas fact", body="New."))
    new_raw = _read(tenant, "new-atlas.md")
    old_raw = _read(tenant, "old-atlas.md")
    assert "/old-atlas" in okf.cross_links(new_raw) and "*supersedes*" in new_raw
    assert "/new-atlas" in okf.cross_links(old_raw) and "*superseded by*" in old_raw


# ── addressable by path; body round-trips clean ────────────────────────────


def test_addressable_by_path_and_clean_round_trip(tenant):
    page = Page(id="atlas", title="Atlas uses Redis", body="Atlas uses Redis as its cache.",
                relations=[{"s": "project:atlas", "p": "uses", "o": "library:redis"}])
    store.write_page(page)
    # The concept id is the bundle path (file stem).
    assert okf.concept_id((tenant.pages_dir / "atlas.md"), tenant.pages_dir) == "atlas"
    got = store.read_page("atlas")
    assert got.id == "atlas"
    assert got.body == "Atlas uses Redis as its cache."  # generated links stripped on read
    assert got.relations == page.relations               # extension preserved verbatim


# ── reserved files generated conformantly ──────────────────────────────────


def test_reserved_files_are_generated_and_conformant(tenant):
    store.write_page(Page(id="a", title="Alpha", body="A."))
    store.write_page(Page(id="b", title="Beta", body="B."))

    index = _read(tenant, "index.md")
    assert okf.validate_document(index, path="index.md").conformant
    assert not index.lstrip().startswith("---")             # OKF: index.md has NO frontmatter
    assert "[Alpha](/a)" in index and "[Beta](/b)" in index  # bundle-absolute concept links

    log = _read(tenant, "log.md")
    assert okf.validate_document(log, path="log.md").conformant
    import re
    assert re.search(r"^## \d{4}-\d{2}-\d{2}$", log, re.MULTILINE)  # ISO 8601 date heading
    assert "mnesis: write b" in log                                 # change history from git

    # The whole pages/ bundle validates (reserved files are not counted as concepts).
    bundle = okf.validate_bundle(tenant.pages_dir)
    assert bundle.conformant and bundle.documents == 2


# ── existing behaviour preserved: one commit per write; reserved files ride along ──


def test_one_commit_per_write_includes_reserved_files(tenant):
    assert _commit_count(tenant) == 0
    store.write_page(Page(id="a", title="Alpha", body="A."))
    assert _commit_count(tenant) == 1                       # still exactly one commit
    store.write_page(Page(id="b", title="Beta", body="B."))
    assert _commit_count(tenant) == 2
    # index.md + log.md are committed in the same commit as the page (no extra commits).
    files = subprocess.run(
        ["git", "-C", str(tenant.root_path), "show", "--name-only", "--format=", "HEAD"],
        capture_output=True, text=True).stdout.split()
    assert {"pages/b.md", "pages/index.md", "pages/log.md"} <= set(files)


def test_reserved_files_are_not_pages(tenant):
    store.write_page(Page(id="a", title="Alpha", body="A."))
    ids = {p.id for p in store.list_pages()}
    assert ids == {"a"}                                     # index/log excluded from pages
    with pytest.raises(FileNotFoundError):
        store.read_page("index")
    with pytest.raises(FileNotFoundError):
        store.read_page("log")
