"""OKF3 — lossless, reversible, idempotent, per-tenant migration to OKF.

A seeded **pre-OKF** corpus (the old frontmatter shape) migrates so every entry becomes
OKF-conformant, with **zero data loss**: every field, source, relation, and supersession
link is preserved (timestamps are NOT refreshed, so confidence/decay/supersession
*semantics* are untouched — only the representation changes). A re-run is a no-op;
rollback restores the original byte-for-byte; and one tenant's migration never touches
another's.
"""

from __future__ import annotations

import subprocess

import pytest

from mnesis import config, okf, store, tenancy
from mnesis.store import Page

# Two pre-OKF pages (old shape: `updated`, no `type`/`description`/`timestamp`), where B
# supersedes A — a supersession chain to preserve across the migration.
OLD_A = """---
id: atlas
title: Project Atlas uses Redis for caching
created: '2026-01-01T00:00:00.000000Z'
updated: '2026-02-02T00:00:00.000000Z'
sources:
- atlas-notes
source_count: 2
last_confirmed: '2026-02-02T00:00:00.000000Z'
tags:
- project:atlas
- library:redis
kind: fact
status: stale
owner_principal: null
visibility: shared
supersedes: null
superseded_by: new-atlas
contradicts: []
decay_class: null
relations:
- {s: project:atlas, p: uses, o: library:redis}
---
Project Atlas uses Redis as its primary caching layer.

Source: atlas-notes.
"""

OLD_B = """---
id: new-atlas
title: Atlas moved to Memcached
created: '2026-03-03T00:00:00.000000Z'
updated: '2026-03-03T00:00:00.000000Z'
sources: [migration-note]
source_count: 1
last_confirmed: '2026-03-03T00:00:00.000000Z'
tags: [project:atlas, library:memcached]
kind: fact
status: active
owner_principal: null
visibility: shared
supersedes: atlas
superseded_by: null
contradicts: []
decay_class: null
relations:
- {s: project:atlas, p: uses, o: library:memcached}
---
Atlas now uses Memcached.
"""


def _seed_pre_okf(ctx, files: dict[str, str]) -> None:
    """Write raw pre-OKF page files into a tenant bundle and commit them."""
    ctx.pages_dir.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (ctx.pages_dir / name).write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", str(ctx.root_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(ctx.root_path), "commit", "-q", "-m", "seed pre-OKF"], check=True)


@pytest.fixture()
def seeded(tenant):
    """The bound `default` tenant seeded with the pre-OKF corpus above."""
    _seed_pre_okf(tenant, {"atlas.md": OLD_A, "new-atlas.md": OLD_B})
    return tenant


# ── migration makes everything OKF-conformant ──────────────────────────────


def test_migration_makes_bundle_conformant(seeded):
    # Pre-migration: NOT conformant (no `type`).
    assert not okf.validate_document(OLD_A, path="atlas.md").conformant

    rep = store.migrate_to_okf()
    assert rep["committed"] and set(rep["converted"]) == {"atlas", "new-atlas"}

    bundle = okf.validate_bundle(seeded.pages_dir)
    assert bundle.conformant and bundle.documents == 2  # index/log excluded


# ── zero data loss: every field / source / relation / chain preserved ──────


def test_migration_is_lossless(seeded):
    before_a, before_b = store.read_page("atlas"), store.read_page("new-atlas")
    store.migrate_to_okf()
    a, b = store.read_page("atlas"), store.read_page("new-atlas")

    # Every field round-trips identically (dataclass equality).
    assert a == before_a and b == before_b
    # Timestamps are NOT refreshed — decay/confidence semantics are untouched.
    assert a.updated == "2026-02-02T00:00:00.000000Z"
    assert a.created == "2026-01-01T00:00:00.000000Z"
    # Provenance + confidence inputs preserved.
    assert a.sources == ["atlas-notes"] and a.source_count == 2
    # Supersession chain preserved (state, not just links).
    assert a.status == "stale" and a.superseded_by == "new-atlas" and b.supersedes == "atlas"
    # Relations preserved verbatim.
    assert a.relations == [{"s": "project:atlas", "p": "uses", "o": "library:redis"}]


def test_migration_emits_cross_links_and_reserved_files(seeded):
    store.migrate_to_okf()
    raw_a = (seeded.pages_dir / "atlas.md").read_text(encoding="utf-8")
    # OKF cross-links from the relation + the supersession link, resolvable by path.
    links = okf.cross_links(raw_a)
    assert "/project/atlas" in links and "/library/redis" in links and "/new-atlas" in links
    # Reserved files generated conformantly.
    assert okf.validate_document((seeded.pages_dir / "index.md").read_text(), path="index.md").conformant
    assert okf.validate_document((seeded.pages_dir / "log.md").read_text(), path="log.md").conformant
    assert "[Project Atlas uses Redis for caching](/atlas)" in (seeded.pages_dir / "index.md").read_text()


# ── idempotent: a re-run is a no-op ────────────────────────────────────────


def test_migration_is_idempotent(seeded):
    def _commits() -> int:
        out = subprocess.run(["git", "-C", str(seeded.root_path), "rev-list", "--count", "HEAD"],
                             capture_output=True, text=True)
        return int(out.stdout.strip())

    store.migrate_to_okf()
    after_first = _commits()
    a_bytes = (seeded.pages_dir / "atlas.md").read_bytes()

    rep = store.migrate_to_okf()  # re-run
    assert rep["already_conformant"] and not rep["committed"] and rep["converted"] == []
    assert _commits() == after_first                                   # no new commit
    assert (seeded.pages_dir / "atlas.md").read_bytes() == a_bytes      # byte-identical

    # A dry-run also reports nothing to do and writes nothing.
    assert store.migrate_to_okf(dry_run=True)["already_conformant"]


# ── dry-run writes nothing ─────────────────────────────────────────────────


def test_dry_run_changes_nothing(seeded):
    before = (seeded.pages_dir / "atlas.md").read_text(encoding="utf-8")
    rep = store.migrate_to_okf(dry_run=True)
    assert rep["dry_run"] and set(rep["converted"]) == {"atlas", "new-atlas"} and not rep["committed"]
    assert (seeded.pages_dir / "atlas.md").read_text(encoding="utf-8") == before  # untouched
    assert not (seeded.pages_dir / "index.md").exists()                            # nothing generated


# ── reversible: rollback restores the original ─────────────────────────────


def test_rollback_restores_the_original(seeded):
    store.migrate_to_okf()
    assert (seeded.pages_dir / "atlas.md").read_text(encoding="utf-8") != OLD_A  # converted

    res = store.rollback_okf_migration()
    assert res["tenant"] == "default"
    # Byte-exact restore of the pre-migration corpus.
    assert (seeded.pages_dir / "atlas.md").read_text(encoding="utf-8") == OLD_A
    assert (seeded.pages_dir / "new-atlas.md").read_text(encoding="utf-8") == OLD_B


def test_rollback_without_backup_errors(tenant):
    with pytest.raises(store.MigrationError):
        store.rollback_okf_migration()


# ── per-tenant isolation ───────────────────────────────────────────────────


def test_migration_is_per_tenant(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_ROOT", tmp_path / "data", raising=False)
    monkeypatch.setattr(config, "MNESIS_LLM_STUB", True, raising=False)
    a = tenancy.create_tenant("alpha", data_root=config.DATA_ROOT)
    b = tenancy.create_tenant("beta", data_root=config.DATA_ROOT)
    with tenancy.use(a):
        _seed_pre_okf(a, {"atlas.md": OLD_A})
    with tenancy.use(b):
        _seed_pre_okf(b, {"atlas.md": OLD_A})

    # Migrate ONLY alpha.
    with tenancy.use(a):
        store.migrate_to_okf()
        assert okf.validate_bundle(a.pages_dir).conformant

    # Beta is untouched — still the pre-OKF bytes, still non-conformant.
    assert (b.pages_dir / "atlas.md").read_text(encoding="utf-8") == OLD_A
    with tenancy.use(b):
        assert not okf.validate_bundle(b.pages_dir).conformant
