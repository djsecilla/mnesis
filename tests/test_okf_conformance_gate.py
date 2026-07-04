"""OKF7 — the conformance gate (the no-regressions capstone).

Locks OKF conformance in: a non-conformant write **fails closed**; every stored entry
validates as OKF through the *full* lifecycle (ingest → reinforce → supersede →
contradict → file-back → decay → rebuild); exported bundles conform; and OKF is proven
to be **representation only** — confidence/decay/supersession values are byte-identical
before and after migration (no semantic change).
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from mnesis import (
    config, confidence, graph, ingest, lifecycle, mcp_server, okf, okf_bundle,
    search, state, store, tenancy,
)
from mnesis.store import OKFConformanceError, Page

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)
TITLE = "Project Atlas uses Redis for caching"


def _assert_bundle_conformant(ctx, *, where: str = "") -> okf.OKFReport:
    r = okf.validate_bundle(ctx.pages_dir)
    assert r.conformant, f"{where}: non-conformant: {[str(i) for i in r.errors]}"
    # …and every concept validates individually.
    for p in store.list_pages():
        raw = (ctx.pages_dir / f"{p.id}.md").read_text(encoding="utf-8")
        assert okf.validate_document(raw, path=f"{p.id}.md").conformant, f"{where}: {p.id}"
    return r


def _seed_old(ctx, name: str, text: str) -> None:
    ctx.pages_dir.mkdir(parents=True, exist_ok=True)
    (ctx.pages_dir / name).write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", str(ctx.root_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(ctx.root_path), "commit", "-q", "-m", "seed"], check=True)


# ── the gate: a non-conformant write fails closed ──────────────────────────


def test_non_conformant_write_is_rejected(tenant):
    # A page whose OKF `type` (= kind) would be empty is refused BEFORE any write/commit.
    with pytest.raises(OKFConformanceError):
        store.write_page(Page(id="bad", title="No type", body="x", kind=""))
    assert store.list_pages() == []                       # nothing written
    out = subprocess.run(["git", "-C", str(tenant.root_path), "rev-list", "--count", "HEAD"],
                         capture_output=True, text=True)
    assert out.stdout.strip() in ("0", "")                # nothing committed


# ── every stored entry validates through the whole lifecycle ───────────────


def test_every_entry_validates_through_full_lifecycle(tenant):
    ingest.ingest_source(f"{TITLE}. tag{{project:atlas}} rel{{project:atlas|uses|library:redis}}", "s1")
    _assert_bundle_conformant(tenant, where="new")

    r = ingest.ingest_source(f"{TITLE}. relation:reinforces", "s2")
    assert store.read_page(r.id).source_count == 2
    _assert_bundle_conformant(tenant, where="reinforce")

    new = ingest.ingest_source(f"{TITLE}. relation:supersedes Now it uses Memcached.", "s3")
    assert new.supersedes and store.read_page(new.supersedes).status == "stale"
    _assert_bundle_conformant(tenant, where="supersede")

    out = mcp_server.mnesis_file_back("What caches Atlas?", "Atlas uses Redis for caching. " * 6, 0.9)
    assert out.startswith("filed digest")
    _assert_bundle_conformant(tenant, where="file_back")

    lifecycle.recompute_all(now=datetime.now(timezone.utc) + timedelta(days=400))
    _assert_bundle_conformant(tenant, where="decay")

    search.rebuild()
    graph.rebuild_graph()
    _assert_bundle_conformant(tenant, where="rebuild")


def test_contradiction_branches_stay_conformant(tenant):
    def _seed() -> Page:
        p = Page(id="atlas-cache", title=TITLE, body=f"{TITLE} as its layer.", sources=["seed"], kind="fact")
        store.write_page(p)
        search.rebuild()
        return p
    _seed()
    ingest.ingest_source(f"{TITLE}. relation:contradicts It uses Postgres.", "s2")
    assert len(state.list_open_reviews()) == 1            # queued (behaviour unchanged)
    _assert_bundle_conformant(tenant, where="contradiction")


# ── exported bundles conform ───────────────────────────────────────────────


def test_exported_bundles_conform(tenant, tmp_path):
    ingest.ingest_source(f"{TITLE}. rel{{project:atlas|uses|library:redis}}", "s1")
    for fmt, dest in (("dir", tmp_path / "b"), ("tar", tmp_path / "b.tar.gz")):
        rep = okf_bundle.export_bundle(dest, fmt=fmt)
        assert rep["conformant"] and not rep["issues"]
    assert okf.validate_bundle(tmp_path / "b").conformant


# ── OKF is representation only: no semantic change ─────────────────────────


def test_confidence_unchanged_by_migration(tenant):
    lc = "2026-03-01T00:00:00.000000Z"
    _seed_old(tenant, "p.md",
        "---\nid: p\ntitle: T\ncreated: '2026-01-01T00:00:00.000000Z'\n"
        f"updated: '{lc}'\nsources:\n- s\nsource_count: 3\nlast_confirmed: '{lc}'\n"
        "tags: []\nkind: fact\nstatus: active\nowner_principal: null\nvisibility: shared\n"
        "supersedes: null\nsuperseded_by: null\ncontradicts: []\ndecay_class: null\nrelations: []\n---\nBody.\n")

    before = confidence.compute_confidence(store.read_page("p"), access=None, now=NOW)
    store.migrate_to_okf()
    after = confidence.compute_confidence(store.read_page("p"), access=None, now=NOW)
    assert before == after                                # identical score + breakdown


def test_supersession_state_unchanged_by_migration(tenant):
    # A superseded pair round-trips its lifecycle state through migration.
    _seed_old(tenant, "old.md",
        "---\nid: old\ntitle: Old\ncreated: '2026-01-01T00:00:00.000000Z'\nupdated: '2026-01-01T00:00:00.000000Z'\n"
        "sources: [s]\nsource_count: 1\nlast_confirmed: '2026-01-01T00:00:00.000000Z'\ntags: []\nkind: fact\n"
        "status: stale\nowner_principal: null\nvisibility: shared\nsupersedes: null\nsuperseded_by: new\n"
        "contradicts: []\ndecay_class: null\nrelations: []\n---\nOld.\n")
    store.migrate_to_okf()
    old = store.read_page("old")
    assert old.status == "stale" and old.superseded_by == "new"
    _assert_bundle_conformant(tenant, where="post-migration")
