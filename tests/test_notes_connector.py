"""Tests for the SourceConnector pattern + the NotesInboxConnector (W1).

Offline, temp dirs. Validates: a dropped note emits exactly one normalized
InboundEvent (stable source_ref + content_hash); identical content does not
re-emit; an unreadable/oversized file surfaces as an error without stopping the
watch; both poll and watch modes detect new files.
"""
from __future__ import annotations

import asyncio

import pytest

from mnesis_agents.connectors.notes import _content_hash
from mnesis_agents.connectors import NotesInboxConnector
from mnesis_agents.triggers import ConnectorError, InboundEvent, ProcessedStore


def _connector(tmp_path, **kw) -> NotesInboxConnector:
    inbox = tmp_path / "inbox"
    inbox.mkdir(exist_ok=True)
    store = ProcessedStore(tmp_path / "state" / "notes.sqlite")
    kw.setdefault("poll_interval", 0.05)
    return NotesInboxConnector(inbox, processed_store=store, **kw)


async def _collect(connector: NotesInboxConnector, *, n: int = 1, timeout: float = 2.0) -> list:
    """Drain up to ``n`` events (or until timeout), then stop the connector."""
    out: list = []

    async def run():
        async for ev in connector.stream():
            out.append(ev)
            if len(out) >= n:
                break

    try:
        await asyncio.wait_for(run(), timeout)
    except asyncio.TimeoutError:
        pass
    finally:
        await connector.stop()
    return out


# ── normalized event shape ──────────────────────────────────────────────────


def test_dropped_md_emits_one_normalized_event(tmp_path):
    c = _connector(tmp_path)
    (c.inbox / "ideas.md").write_text("Atlas uses Redis for caching.", encoding="utf-8")

    events = asyncio.run(_collect(c, n=1))
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, InboundEvent)
    # Normalized envelope.
    assert ev.source_type == "notes"
    assert ev.source_ref == "note:ideas.md"           # stable, relative-path based
    assert ev.kind == "file_added"
    assert ev.text == "Atlas uses Redis for caching."
    assert ev.content_hash == _content_hash(ev.text)
    assert ev.metadata["rel_path"] == "ideas.md" and ev.metadata["size"] > 0
    # Mirrored onto the generic runner fields.
    assert ev.source == "notes" and ev.payload == ev.text and ev.id == ev.source_ref


def test_source_ref_is_stable_and_path_relative(tmp_path):
    c = _connector(tmp_path)
    (c.inbox / "sub").mkdir()
    (c.inbox / "sub" / "note.txt").write_text("nested note", encoding="utf-8")
    events = asyncio.run(_collect(c, n=1))
    assert events[0].source_ref == "note:sub/note.txt"


# ── idempotency ─────────────────────────────────────────────────────────────


def test_identical_content_does_not_re_emit(tmp_path):
    c = _connector(tmp_path)
    note = c.inbox / "a.md"
    note.write_text("same content", encoding="utf-8")
    first = asyncio.run(_collect(c, n=1))
    assert len(first) == 1

    # Re-drop identical content (overwrite with the same bytes) — no re-emit.
    note.write_text("same content", encoding="utf-8")
    again = asyncio.run(_collect(c, n=1, timeout=0.4))
    assert again == []
    # The ledger recorded it once.
    rows = c._store.all()
    assert len(rows) == 1 and rows[0]["source_ref"] == "note:a.md"


def test_edited_content_re_emits(tmp_path):
    c = _connector(tmp_path)
    note = c.inbox / "a.md"
    note.write_text("version one", encoding="utf-8")
    assert len(asyncio.run(_collect(c, n=1))) == 1

    note.write_text("version two — edited", encoding="utf-8")  # new content_hash
    second = asyncio.run(_collect(c, n=1))
    assert len(second) == 1 and second[0].text == "version two — edited"


def test_ack_marks_processed(tmp_path):
    c = _connector(tmp_path)
    (c.inbox / "a.md").write_text("hello", encoding="utf-8")
    ev = asyncio.run(_collect(c, n=1))[0]
    asyncio.run(c.ack(ev))
    row = c._store.all()[0]
    assert row["status"] == "processed"


# ── resilience ──────────────────────────────────────────────────────────────


def test_unreadable_file_surfaces_error_without_stopping(tmp_path):
    c = _connector(tmp_path)
    # A bad (undecodable) file alongside a good one.
    (c.inbox / "bad.md").write_bytes(b"\xff\xfe\x00\x00 not utf-8 \xff")
    (c.inbox / "good.md").write_text("a good note", encoding="utf-8")

    events = asyncio.run(_collect(c, n=1))
    # The good note still came through…
    assert len(events) == 1 and events[0].source_ref == "note:good.md"
    # …and the bad file surfaced as an error, not a crash.
    assert any(e.error == "unreadable" and e.source_ref == "note:bad.md" for e in c.errors)


def test_oversized_file_surfaces_error(tmp_path):
    c = _connector(tmp_path, max_bytes=16)
    (c.inbox / "big.md").write_text("x" * 100, encoding="utf-8")
    (c.inbox / "ok.md").write_text("small", encoding="utf-8")

    events = asyncio.run(_collect(c, n=1))
    assert len(events) == 1 and events[0].source_ref == "note:ok.md"
    assert any(e.error == "oversized" and e.source_ref == "note:big.md" for e in c.errors)


def test_error_handler_is_called(tmp_path):
    seen: list[ConnectorError] = []
    c = _connector(tmp_path, max_bytes=16, error_handler=seen.append)
    (c.inbox / "big.md").write_text("x" * 100, encoding="utf-8")  # oversized
    events = asyncio.run(_collect(c, n=1, timeout=0.4))
    assert events == []  # nothing emittable
    assert seen and seen[0].error == "oversized"


# ── poll vs watch modes ─────────────────────────────────────────────────────


def test_poll_mode_detects_a_file_added_after_start(tmp_path):
    c = _connector(tmp_path, mode="poll")

    async def scenario():
        out: list = []

        async def consume():
            async for ev in c.stream():
                out.append(ev)
                break

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.12)  # let the poll loop spin
        (c.inbox / "late.md").write_text("arrived after start", encoding="utf-8")
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except asyncio.TimeoutError:
            task.cancel()
        await c.stop()
        return out

    events = asyncio.run(scenario())
    assert len(events) == 1 and events[0].source_ref == "note:late.md"


def test_watch_mode_detects_a_file_added_after_start(tmp_path):
    pytest.importorskip("watchdog")
    c = _connector(tmp_path, mode="watch")

    async def scenario():
        out: list = []

        async def consume():
            async for ev in c.stream():
                out.append(ev)
                break

        task = asyncio.create_task(consume())
        await c.start()
        await asyncio.sleep(0.2)  # let the observer attach
        (c.inbox / "watched.md").write_text("filesystem event", encoding="utf-8")
        try:
            await asyncio.wait_for(task, timeout=3.0)
        except asyncio.TimeoutError:
            task.cancel()
        await c.stop()
        return out

    events = asyncio.run(scenario())
    assert len(events) == 1 and events[0].source_ref == "note:watched.md"
