"""Phase-2 end-to-end regression: the full confidence/lifecycle loop (stub mode).

reinforce -> supersede -> query -> contradiction review/resolve -> decay, plus
the refined canonical-vs-cache invariant: deleting the search index and
rebuilding preserves the durable state store (access + reviews) and reproduces
ranking and confidences.
"""

from __future__ import annotations

import subprocess

import pytest

from mnesis import config, ingest, lifecycle, mcp_server, search, state, store
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
    monkeypatch.setattr(config, "STALE_THRESHOLD", 0.5)  # so an aged 1-src page can go stale

    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@localhost"], check=True
    )
    return tmp_path


def _conf(page_id: str) -> float:
    from mnesis import confidence

    page = store.read_page(page_id)
    return confidence.compute_confidence(page, access=state.get_access(page_id))[0]


def test_phase2_full_lifecycle(wiki):
    # 1. ingest a claim -> page A, moderate confidence.
    a = ingest.ingest_source(f"{TITLE}.", "atlas-arch")
    assert a.kind == "fact" and a.source_count == 1
    conf_a0 = _conf(a.id)
    assert 0.7 < conf_a0 < 0.8

    # 2. agreeing source -> A reinforced (one page, more support, higher confidence).
    r = ingest.ingest_source(f"{TITLE}. relation:reinforces", "atlas-confirm")
    assert r.id == a.id
    assert store.read_page(a.id).source_count == 2
    assert len(store.list_pages()) == 1
    assert _conf(a.id) > conf_a0

    # 3. updating source -> B supersedes A; A goes stale.
    b = ingest.ingest_source(f"{TITLE}. relation:supersedes Atlas now uses Memcached.", "atlas-upd")
    assert b.id != a.id
    assert b.supersedes == a.id
    assert store.read_page(a.id).status == "stale"
    assert store.read_page(a.id).superseded_by == b.id

    # 4. query the topic -> B ranks; A excluded by default, demoted with include_stale.
    assert [h.id for h in search.search("redis caching")] == [b.id]
    with_stale = [h.id for h in search.search("redis caching", include_stale=True)]
    assert with_stale[0] == b.id and a.id in with_stale

    # 5. low-margin conflicting source -> review queue -> resolve.
    c = ingest.ingest_source(f"{TITLE}. relation:contradicts Atlas uses Postgres.", "atlas-conf")
    assert b.id in c.contradicts
    assert len(state.list_open_reviews()) == 1
    review_id = state.list_open_reviews()[0]["id"]
    conf_b_penalised = _conf(b.id)

    mcp_server.mnesis_resolve(review_id, b.id)
    assert store.read_page(c.id).status == "stale"
    assert store.read_page(c.id).superseded_by == b.id
    assert b.id not in store.read_page(c.id).contradicts
    assert _conf(b.id) > conf_b_penalised  # penalty lifted
    assert state.list_open_reviews() == []

    # 6. decay over an aged fixture page -> it transitions to stale (idempotently).
    aged = Page(
        id="legacy",
        title="Legacy runbook",
        body="An old unread runbook.",
        sources=["legacy"],
        source_count=1,
        last_confirmed="2024-01-01T00:00:00.000000Z",
    )
    store.write_page(aged)
    search.upsert(aged)
    summary = lifecycle.recompute_all()
    assert summary["restaled"] >= 1
    assert store.read_page("legacy").status == "stale"
    # active pages (B) stay active; idempotent second run makes no transitions.
    assert store.read_page(b.id).status == "active"
    again = lifecycle.recompute_all()
    assert again["restaled"] == 0 and again["reactivated"] == 0


def test_rebuild_preserves_state_and_reproduces_ranking(wiki):
    a = ingest.ingest_source(f"{TITLE}.", "atlas-arch")
    ingest.ingest_source("Billing runs on PostgreSQL with nightly backups.", "billing")
    # Build durable state: an access record and an (unresolved) review entry.
    mcp_server.mnesis_get(a.id)
    mcp_server.mnesis_get(a.id)
    review_id = state.enqueue_contradiction(a.id, "ghost-page", "fixture review")

    access_before = state.get_access(a.id)
    open_before = state.list_open_reviews()
    ranking_before = [
        (h.id, round(h.confidence, 4)) for h in search.search("the", include_stale=True)
    ]

    # Delete ONLY the rebuildable search index; the durable state store stays.
    (config.INDEX_DIR / "wiki.db").unlink()
    assert (config.INDEX_DIR / "state.db").exists()
    search.rebuild()

    # Durable state survived untouched.
    assert state.get_access(a.id) == access_before
    assert state.list_open_reviews() == open_before
    assert any(r["id"] == review_id for r in state.list_open_reviews())

    # Ranking and confidences reproduced (confidence's tiny time-drift tolerated).
    ranking_after = [
        (h.id, round(h.confidence, 4)) for h in search.search("the", include_stale=True)
    ]
    assert [r[0] for r in ranking_after] == [r[0] for r in ranking_before]
    for (_, cb), (_, ca) in zip(ranking_before, ranking_after):
        assert abs(ca - cb) < 0.01
