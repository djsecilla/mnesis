"""OKF5 — ingestion and writing agents produce OKF-conformant entries.

New knowledge enters Mnesis only through the ingestion pipeline, which writes via the
OKF-conformant store (OKF2). So every ingested page — and every page a writing agent
produces (it just calls `mnesis_ingest` server-side) — is OKF-conformant, **with all
ingestion behavior unchanged**: redaction, plan/apply, routing (new/reinforce/supersede/
contradict), the review queue, and per-tenant governance behave exactly as before.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from mnesis import config, ingest, okf, search, state, store
from mnesis.store import Page

TITLE = "Project Atlas uses Redis for caching"
SECRET = "sk-ABCDEF0123456789abcdef"


def _raw(tenant, page_id: str) -> str:
    return (tenant.pages_dir / f"{page_id}.md").read_text(encoding="utf-8")


def _seed_existing(days_old: int = 0) -> Page:
    lc = (datetime.now(timezone.utc) - timedelta(days=days_old)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    page = Page(id="atlas-cache", title=TITLE, body=f"{TITLE} as its primary layer.",
                sources=["seed-source"], last_confirmed=lc, kind="fact")
    store.write_page(page)
    search.rebuild()
    return page


# ── an ingested page is OKF-conformant, valid type, cross-links, redacted ──


def test_ingest_yields_okf_page_with_type_and_cross_links(tenant):
    # `tag{…}`/`rel{…}` markers drive the offline extractor; a secret is redacted.
    text = (f"{TITLE}. tag{{project:atlas}} tag{{library:redis}} "
            f"rel{{project:atlas|uses|library:redis}} Deploy key {SECRET}.")
    page = ingest.ingest_source(text, "atlas-notes")

    raw = _raw(tenant, page.id)
    assert okf.validate_document(raw, path=f"{page.id}.md").conformant  # every entry validates
    assert page.kind in {"fact", "digest", "note"} and "type: fact" in raw  # a valid OKF type
    assert "description:" in raw and "timestamp:" in raw                    # OKF core fields set
    # OKF cross-links emitted from the extracted relation.
    links = okf.cross_links(raw)
    assert "/project/atlas" in links and "/library/redis" in links
    # Redaction unchanged: the secret is nowhere (page or the persisted source).
    assert SECRET not in raw
    assert SECRET not in (tenant.sources_dir / "atlas-notes.md").read_text(encoding="utf-8")


# ── routing preserved — and every routed page stays OKF ────────────────────


def test_reinforce_preserved_and_okf(tenant):
    existing = _seed_existing()
    result = ingest.ingest_source(f"{TITLE}. relation:reinforces", "src-2")
    # Behaviour unchanged: same page, source_count bumped, no duplicate.
    assert result.id == existing.id and len(store.list_pages()) == 1
    assert store.read_page(existing.id).source_count == 2
    assert okf.validate_bundle(tenant.pages_dir).conformant


def test_supersede_preserved_and_okf(tenant):
    existing = _seed_existing()
    new = ingest.ingest_source(f"{TITLE}. relation:supersedes Now it uses Memcached.", "src-2")
    # Behaviour unchanged: new active supersedes old; old → stale.
    assert new.supersedes == existing.id and new.status == "active"
    assert store.read_page(existing.id).status == "stale"
    # Both pages OKF, and the supersession is an OKF cross-link on the new page.
    assert okf.validate_bundle(tenant.pages_dir).conformant
    assert f"/{existing.id}" in okf.cross_links(_raw(tenant, new.id))


def test_low_margin_contradiction_queues_and_okf(tenant):
    existing = _seed_existing()  # fresh → comparable confidence → queued (not auto-resolved)
    new = ingest.ingest_source(
        f"{TITLE}. relation:contradicts It actually uses Postgres for caching.", "src-2")
    # Behaviour unchanged: both coexist, cross-linked in `contradicts`, one queued review.
    assert existing.id in new.contradicts and new.id in store.read_page(existing.id).contradicts
    reviews = state.list_open_reviews()
    assert len(reviews) == 1 and {reviews[0]["page_a"], reviews[0]["page_b"]} == {new.id, existing.id}
    assert okf.validate_bundle(tenant.pages_dir).conformant


def test_clear_margin_contradiction_auto_supersedes_and_okf(tenant):
    existing = _seed_existing(days_old=200)  # weak → auto-superseded
    new = ingest.ingest_source(f"{TITLE}. relation:contradicts It now uses Postgres.", "src-2")
    assert new.supersedes == existing.id and store.read_page(existing.id).status == "stale"
    assert state.list_open_reviews() == []  # auto-resolved, nothing queued
    assert okf.validate_bundle(tenant.pages_dir).conformant


def test_unrelated_creates_fresh_okf_page(tenant):
    _seed_existing()
    new = ingest.ingest_source(f"{TITLE}. relation:unrelated", "src-2")
    assert new.id != "atlas-cache" and len(store.list_pages()) == 2
    assert okf.validate_bundle(tenant.pages_dir).conformant


# ── plan/apply seam still writes OKF ───────────────────────────────────────


def test_plan_apply_writes_okf(tenant):
    plan = ingest.plan_ingest(f"{TITLE}. rel{{project:atlas|uses|library:redis}}", "atlas-notes")
    assert store.list_pages() == []          # plan writes nothing (unchanged)
    result = ingest.apply_ingest(plan)
    raw = _raw(tenant, result["page_id"])
    assert okf.validate_document(raw, path=f"{result['page_id']}.md").conformant
    assert "/library/redis" in okf.cross_links(raw)


# ── a writing-agent run produces OKF-conformant output ─────────────────────


def test_writing_agent_output_is_okf(tenant, tmp_path):
    pytest.importorskip("langchain_core")
    from langchain_core.tools import tool

    from mnesis import mcp_server
    from mnesis_agents.audit import AgentAuditLog
    from mnesis_agents.skills.loader import SkillRegistry
    from mnesis_agents.triggers.connector import ProcessedStore
    from mnesis_agents.triggers.events import InboundEvent
    from mnesis_agents.writing_agent import SourceWritingAgent

    @tool
    def mnesis_ingest(text: str, source_ref: str) -> str:
        """Ingest a source into Mnesis (filtered, extracted, routed) — the REAL server tool."""
        return mcp_server.mnesis_ingest(text, source_ref)  # runs under the bound tenant

    agent = SourceWritingAgent(
        tools=[mnesis_ingest],
        skills=SkillRegistry().discover(),
        processed_store=ProcessedStore(tmp_path / "processed.sqlite"),
        audit=AgentAuditLog(tmp_path),
    )
    note = InboundEvent.from_source(
        source_type="notes", source_ref="note:atlas.md", kind="file_added",
        text=f"{TITLE}. rel{{project:atlas|uses|library:redis}}",
        content_hash="h1", metadata={"rel_path": "atlas.md"},
    )
    result = agent.handle_event(note)
    assert result.status == "ingested" and result.page_id

    # The page the agent produced (server-side) is OKF-conformant with a valid type.
    raw = _raw(tenant, result.page_id)
    report = okf.validate_document(raw, path=f"{result.page_id}.md")
    assert report.conformant and "type: fact" in raw
    assert "/library/redis" in okf.cross_links(raw)  # cross-links flow through
