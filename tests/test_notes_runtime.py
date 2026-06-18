"""End-to-end (stub) wiring of the notes-inbox connector + WritingAgent into the
F5 runner (W5). Offline: a recording fake ``mnesis_ingest`` + the parse-note skill
+ temp stores. Proves a dropped note is ingested over the runtime, a re-drop does
not duplicate, and a malformed note dead-letters — never a crash, never silent loss.
"""
from __future__ import annotations

import asyncio

from langchain_core.tools import tool

from mnesis_agents import cli, config
from mnesis_agents.audit import AgentAuditLog
from mnesis_agents.connectors.notes import NotesInboxConnector
from mnesis_agents.registry import AgentRegistry
from mnesis_agents.runner import Runner
from mnesis_agents.skills.loader import SkillRegistry
from mnesis_agents.triggers.connector import ProcessedStore
from mnesis_agents.writing_agent import SourceWritingAgent
from mnesis_agents.writing_pipeline import DeadLetterStore, PipelineConfig, WritingPipeline


def _ingest_tool(calls: list):
    @tool
    def mnesis_ingest(text: str, source_ref: str) -> str:
        """ingest"""
        calls.append({"text": text, "source_ref": source_ref})
        return "ingested page: p\naction: new\nredactions: 0"

    return mnesis_ingest


def _wire(tmp_path, calls):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    store = ProcessedStore(tmp_path / "state.sqlite")
    agent = SourceWritingAgent(
        tools=[_ingest_tool(calls)], skills=SkillRegistry().discover(),
        processed_store=store, audit=AgentAuditLog(tmp_path),
    )
    connector = NotesInboxConnector(inbox, processed_store=store, mode="poll", poll_interval=0.05)
    pipeline = WritingPipeline(
        agent, dead_letter=DeadLetterStore(tmp_path),
        config=PipelineConfig(max_retries=1, backoff_base=0.001, backoff_factor=1.5),
    )
    registry = AgentRegistry()
    conn, sub, pipe = cli.register_notes_writer(
        registry, connector=connector, agent=agent, pipeline=pipeline
    )
    assert sub.name == "notes-writer" and sub.source == "notes"
    return inbox, Runner(registry, event_triggers=[conn]), pipe


async def _run_until(runner, predicate, *, timeout=4.0, setup=None):
    await runner.start()
    if setup is not None:
        setup()
    try:
        for _ in range(int(timeout / 0.05)):
            await asyncio.sleep(0.05)
            if predicate():
                break
    finally:
        await runner.stop()


# ── drop → ingest; re-drop → no duplicate ───────────────────────────────────


def test_dropping_a_note_ingests_it_over_the_runtime(tmp_path):
    calls: list = []
    inbox, runner, pipeline = _wire(tmp_path, calls)

    asyncio.run(_run_until(
        runner, lambda: len(calls) >= 1,
        setup=lambda: (inbox / "idea.md").write_text(
            "Project Atlas uses Redis for caching widely.", encoding="utf-8"),
    ))

    assert len(calls) == 1
    assert calls[0]["source_ref"] == "note:idea.md"
    assert calls[0]["text"] == "Project Atlas uses Redis for caching widely."
    assert pipeline.dead_letter.all() == []


def test_re_dropping_identical_content_does_not_duplicate(tmp_path):
    calls: list = []
    inbox, runner, pipeline = _wire(tmp_path, calls)
    note = inbox / "idea.md"

    async def scenario():
        await runner.start()
        note.write_text("Atlas uses Redis for caching widely.", encoding="utf-8")
        for _ in range(60):
            await asyncio.sleep(0.05)
            if calls:
                break
        # Re-drop identical content; give the poller several cycles.
        note.write_text("Atlas uses Redis for caching widely.", encoding="utf-8")
        await asyncio.sleep(0.4)
        await runner.stop()

    asyncio.run(scenario())
    assert len(calls) == 1  # ingested exactly once despite the re-drop


# ── malformed note → dead-letter (no silent loss) ───────────────────────────


def test_malformed_note_dead_letters_without_ingest(tmp_path):
    calls: list = []
    inbox, runner, pipeline = _wire(tmp_path, calls)

    asyncio.run(_run_until(
        runner, lambda: len(pipeline.dead_letter.all()) >= 1,
        setup=lambda: (inbox / "bad.md").write_bytes(b"\xff\xfe\x00 not valid utf-8 \xff"),
    ))

    dead = pipeline.dead_letter.all()
    assert len(dead) == 1
    assert dead[0].source_ref == "note:bad.md"
    assert "connector/unreadable" in dead[0].reason
    assert calls == []  # never ingested


# ── runtime registration resilience ─────────────────────────────────────────


def test_build_runner_registers_notes_writer(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MNESIS_AGENTS_DREAM_ENABLED", False)
    monkeypatch.setattr(config, "MNESIS_NOTES_ENABLED", True)
    monkeypatch.setattr(config, "MNESIS_NOTES_INBOX", tmp_path / "inbox")
    monkeypatch.setattr(config, "MNESIS_AGENTS_CONNECTOR_STATE_DIR", tmp_path / "state")

    calls: list = []
    monkeypatch.setattr(cli, "_load_mcp_tools", lambda: [_ingest_tool(calls)])
    runner = cli._build_runner()
    assert len(runner.event_triggers) == 1
    assert runner.registry.event_subs[0].name == "notes-writer"


def test_build_runner_idle_when_notes_unreachable(monkeypatch):
    monkeypatch.setattr(config, "MNESIS_AGENTS_DREAM_ENABLED", False)
    monkeypatch.setattr(config, "MNESIS_NOTES_ENABLED", True)

    def boom():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(cli, "_load_mcp_tools", boom)
    runner = cli._build_runner()  # resilient: no crash
    assert runner.event_triggers == [] and runner.registry.is_empty
