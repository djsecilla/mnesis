"""Tests for writing-pipeline robustness (W4): dedup, retry/backoff, dead-letter,
batch, on-demand. Offline — recording/flaky/poison fake ``mnesis_ingest`` tools +
the bundled parse-note skill + temp stores.
"""
from __future__ import annotations

import asyncio

import pytest
from langchain_core.tools import tool

from mnesis_agents.audit import AgentAuditLog
from mnesis_agents.skills.loader import SkillRegistry
from mnesis_agents.triggers.connector import ProcessedStore
from mnesis_agents.triggers.events import InboundEvent
from mnesis_agents.writing_agent import SourceWritingAgent
from mnesis_agents.writing_pipeline import (
    DeadLetterStore,
    PipelineConfig,
    WritingPipeline,
    ingest_note_paths,
)

_OK = "ingested page: p\naction: new\nredactions: 0"


def _ok_tool(calls: list | None = None):
    @tool
    def mnesis_ingest(text: str, source_ref: str) -> str:
        """ingest"""
        if calls is not None:
            calls.append(source_ref)
        return _OK

    return mnesis_ingest


def _flaky_tool(fail_first: int):
    state = {"n": fail_first}

    @tool
    def mnesis_ingest(text: str, source_ref: str) -> str:
        """ingest"""
        if state["n"] > 0:
            state["n"] -= 1
            raise RuntimeError("Mnesis momentarily unavailable")
        return _OK

    return mnesis_ingest


def _poison_tool(marker: str, calls: list | None = None):
    @tool
    def mnesis_ingest(text: str, source_ref: str) -> str:
        """ingest"""
        if calls is not None:
            calls.append(source_ref)
        if marker in source_ref:
            raise RuntimeError("poison ingest")
        return _OK

    return mnesis_ingest


def _agent(tmp_path, ingest_tool, **kw):
    return SourceWritingAgent(
        tools=[ingest_tool], skills=SkillRegistry().discover(),
        processed_store=ProcessedStore(tmp_path / "processed.sqlite"),
        audit=AgentAuditLog(tmp_path), **kw,
    )


def _pipeline(agent, tmp_path, *, max_retries=3, concurrency=4):
    return WritingPipeline(
        agent, dead_letter=DeadLetterStore(tmp_path),
        config=PipelineConfig(max_retries=max_retries, backoff_base=0.001,
                              backoff_factor=1.5, concurrency=concurrency),
    )


def _ev(ref, *, text="Atlas uses Redis for caching widely.", h=None):
    return InboundEvent.from_source(
        source_type="notes", source_ref=ref, kind="file_added",
        text=text, content_hash=h or f"h-{ref}", metadata={},
    )


# ── effectively-once / dedup ────────────────────────────────────────────────


def test_identical_content_delivered_twice_ingests_once(tmp_path):
    calls: list = []
    p = _pipeline(_agent(tmp_path, _ok_tool(calls)), tmp_path)
    ev = _ev("note:a.md")

    r1 = asyncio.run(p.process_event(ev))
    r2 = asyncio.run(p.process_event(ev))  # re-delivery, identical (source_ref, content_hash)
    assert r1.status == "ingested" and r2.status == "duplicate"
    assert calls == ["note:a.md"]  # ingested exactly once


def test_new_but_overlapping_content_flows_through(tmp_path):
    # Same ref, DIFFERENT content_hash (an edit) → not a dedup; flows to Mnesis,
    # whose reinforce logic handles same-claim duplication.
    calls: list = []
    p = _pipeline(_agent(tmp_path, _ok_tool(calls)), tmp_path)
    asyncio.run(p.process_event(_ev("note:a.md", h="h1")))
    asyncio.run(p.process_event(_ev("note:a.md", text="Atlas uses Redis, confirmed again.", h="h2")))
    assert calls == ["note:a.md", "note:a.md"]  # both reached ingest


# ── retry / backoff ─────────────────────────────────────────────────────────


def test_transient_failure_retries_then_succeeds(tmp_path):
    p = _pipeline(_agent(tmp_path, _flaky_tool(2)), tmp_path, max_retries=3)
    r = asyncio.run(p.process_event(_ev("note:a.md")))
    assert r.status == "ingested" and r.attempts == 3  # 2 failures + success
    assert p.dead_letter.all() == []


# ── dead-letter ─────────────────────────────────────────────────────────────


def test_persistent_failure_dead_letters_while_pipeline_continues(tmp_path):
    p = _pipeline(_agent(tmp_path, _poison_tool("poison")), tmp_path, max_retries=2)
    events = [_ev("note:good1.md"), _ev("note:poison.md"), _ev("note:good2.md")]

    results = asyncio.run(p.process_batch(events))
    by_ref = {r.source_ref: r for r in results}
    assert by_ref["note:good1.md"].status == "ingested"
    assert by_ref["note:good2.md"].status == "ingested"  # not blocked by the poison item

    poison = by_ref["note:poison.md"]
    assert poison.status == "dead_letter" and poison.attempts == 3  # 1 + 2 retries
    assert poison.error and "poison" in poison.error

    dl = p.dead_letter.all()
    assert len(dl) == 1 and dl[0].source_ref == "note:poison.md"
    assert dl[0].reason and dl[0].attempts == 3


def test_dead_lettered_item_is_not_reprocessed(tmp_path):
    calls: list = []
    p = _pipeline(_agent(tmp_path, _poison_tool("poison", calls)), tmp_path, max_retries=1)
    ev = _ev("note:poison.md")

    first = asyncio.run(p.process_event(ev))
    assert first.status == "dead_letter"
    n_after_first = len(calls)  # ingest attempts so far (1 + 1 retry = 2)

    again = asyncio.run(p.process_event(ev))  # re-delivery of a known poison item
    assert again.status == "dead_letter" and "already in dead-letter" in (again.error or "")
    assert len(calls) == n_after_first  # NOT retried again


def test_non_retryable_error_dead_letters_immediately(tmp_path):
    # An unmapped source_type is a config/permanent error — dead-letter at once.
    p = _pipeline(_agent(tmp_path, _ok_tool(), parse_skills={"notes": "parse-note"}), tmp_path)
    ev = InboundEvent.from_source(
        source_type="email", source_ref="email:1", kind="message",
        text="some real content here about systems", content_hash="he", metadata={},
    )
    r = asyncio.run(p.process_event(ev))
    assert r.status == "dead_letter" and r.attempts == 1
    assert "no parse skill" in (r.error or "")


# ── batch / burst ───────────────────────────────────────────────────────────


def test_burst_of_files_all_process(tmp_path):
    calls: list = []
    p = _pipeline(_agent(tmp_path, _ok_tool(calls)), tmp_path, concurrency=4)
    events = [_ev(f"note:n{i}.md", text=f"Substantive note number {i} about systems and Redis.")
              for i in range(12)]

    results = asyncio.run(p.process_batch(events))
    assert len(results) == 12 and all(r.status == "ingested" for r in results)
    assert len(calls) == 12 and len(set(calls)) == 12  # each ingested once


# ── on-demand (file + directory) ────────────────────────────────────────────


def test_on_demand_ingests_a_file_and_a_directory(tmp_path):
    inbox = tmp_path / "in"
    (inbox / "sub").mkdir(parents=True)
    (inbox / "one.md").write_text("Atlas uses Redis for caching widely.", encoding="utf-8")
    (inbox / "sub" / "two.txt").write_text(
        "Postgres backups run nightly across all clusters.", encoding="utf-8")

    calls: list = []
    agent = _agent(tmp_path, _ok_tool(calls))
    p = _pipeline(agent, tmp_path)

    # A single file.
    rf = asyncio.run(ingest_note_paths([inbox / "one.md"], agent=agent, pipeline=p))
    assert len(rf) == 1 and rf[0].status == "ingested" and rf[0].source_ref == "note:one.md"

    # A directory (recursive): one.md is now a duplicate; the new file ingests.
    rd = asyncio.run(ingest_note_paths([inbox], agent=agent, pipeline=p))
    by_ref = {r.source_ref: r.status for r in rd}
    assert by_ref.get("note:one.md") == "duplicate"
    assert by_ref.get("note:sub/two.txt") == "ingested"


def test_cli_ingest_note(tmp_path, monkeypatch, capsys):
    from mnesis_agents import cli, config

    monkeypatch.setattr(config, "MNESIS_AGENTS_DEAD_LETTER_DIR", tmp_path)
    monkeypatch.setattr(cli, "_build_writing_agent", lambda: _agent(tmp_path, _ok_tool()))

    inbox = tmp_path / "in"
    inbox.mkdir()
    (inbox / "a.md").write_text("Atlas uses Redis for caching widely.", encoding="utf-8")

    assert cli.main(["ingest-note", str(inbox / "a.md")]) == 0
    out = capsys.readouterr().out
    assert "ingested" in out and "note:a.md" in out and "summary" in out
