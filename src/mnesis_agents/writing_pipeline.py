"""Writing-pipeline robustness (W4): dedup, retry/backoff, dead-letter, batch.

The connector→agent path must be **effectively-once** with **no silent loss**:

  - **Dedup / effectively-once** — the connector delivers at-least-once and the
    agent processes idempotently, keyed by **``(source_ref, content_hash)``**: an
    item already ``processed`` is a no-op ``duplicate``. (Genuinely
    new-but-overlapping sources — a *different* hash — still flow to Mnesis, whose
    reinforce logic handles same-claim duplication.)
  - **Retry/backoff** — a *transient* failure (e.g. Mnesis momentarily
    unavailable → the ingest tool raised) is retried with exponential backoff.
  - **Dead-letter** — a *poison* item (a non-transient error, or one that keeps
    failing after the retry budget) is recorded in a durable
    :class:`DeadLetterStore` **with a reason** and skipped on re-delivery — the
    pipeline never wedges and never silently drops.
  - **Batch** — a burst processes with **bounded concurrency** and **isolation**:
    one poison item never blocks the rest.

The pipeline owns *only* delivery robustness; the per-item flow (parse → govern →
ingest → ack → audit) stays in :class:`SourceWritingAgent`. ``ingest_note_paths``
is the on-demand entry the CLI uses to backfill a file or directory immediately.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from . import config
from .config import now_iso as _now

if TYPE_CHECKING:
    from .triggers.events import InboundEvent
    from .writing_agent import SourceWritingAgent, WritingResult


def _key(source_ref: str | None, content_hash: str | None) -> str:
    blob = f"{source_ref}\n{content_hash}"
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


# ── Dead-letter store ───────────────────────────────────────────────────────


@dataclass
class DeadLetterEntry:
    id: str
    source_type: str | None
    source_ref: str | None
    content_hash: str | None
    reason: str
    attempts: int
    first_seen: str
    updated: str


class DeadLetterStore:
    """Append-with-upsert JSONL ledger of poison items, keyed by
    ``(source_ref, content_hash)`` — one entry per poison item (latest wins), with
    the failure reason and the attempt count. Durable, inspectable, gitignored."""

    def __init__(self, directory: Path | str | None = None, *, filename: str = "dead-letter.jsonl") -> None:
        from .triggers.connector import path_lock

        self.directory = Path(directory or config.MNESIS_AGENTS_DEAD_LETTER_DIR)
        self._path = self.directory / filename
        # Guard the read-modify-write against concurrent batch workers.
        self._lock = path_lock(self._path)

    def _load(self) -> dict[str, DeadLetterEntry]:
        out: dict[str, DeadLetterEntry] = {}
        if not self._path.is_file():
            return out
        with open(self._path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rec = json.loads(line)
                    out[rec["id"]] = DeadLetterEntry(**rec)
        return out

    def _rewrite(self, items: dict[str, DeadLetterEntry]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            for e in sorted(items.values(), key=lambda e: (e.first_seen, e.id)):
                fh.write(json.dumps(asdict(e), ensure_ascii=False) + "\n")
        tmp.replace(self._path)

    def contains(self, source_ref: str | None, content_hash: str | None) -> bool:
        return _key(source_ref, content_hash) in self._load()

    def add(
        self, *, source_type: str | None, source_ref: str | None, content_hash: str | None,
        reason: str, attempts: int,
    ) -> DeadLetterEntry:
        with self._lock:
            items = self._load()
            kid = _key(source_ref, content_hash)
            now = _now()
            existing = items.get(kid)
            entry = DeadLetterEntry(
                id=kid, source_type=source_type, source_ref=source_ref, content_hash=content_hash,
                reason=reason, attempts=attempts,
                first_seen=existing.first_seen if existing else now, updated=now,
            )
            items[kid] = entry
            self._rewrite(items)
        return entry

    def all(self) -> list[DeadLetterEntry]:
        return sorted(self._load().values(), key=lambda e: (e.first_seen, e.id))


# ── Pipeline ────────────────────────────────────────────────────────────────


@dataclass
class PipelineConfig:
    max_retries: int = field(default_factory=lambda: config.MNESIS_AGENTS_WRITE_MAX_RETRIES)
    backoff_base: float = field(default_factory=lambda: config.MNESIS_AGENTS_WRITE_BACKOFF_BASE)
    backoff_factor: float = field(default_factory=lambda: config.MNESIS_AGENTS_WRITE_BACKOFF_FACTOR)
    concurrency: int = field(default_factory=lambda: config.MNESIS_AGENTS_WRITE_CONCURRENCY)


class WritingPipeline:
    """Delivery-robustness wrapper around a :class:`SourceWritingAgent`."""

    def __init__(
        self,
        agent: "SourceWritingAgent",
        *,
        dead_letter: DeadLetterStore | None = None,
        config: PipelineConfig | None = None,
    ) -> None:
        self._agent = agent
        self._dl = dead_letter if dead_letter is not None else DeadLetterStore()
        self._cfg = config or PipelineConfig()

    @property
    def dead_letter(self) -> DeadLetterStore:
        return self._dl

    async def process_event(self, event: "InboundEvent", *, approved: bool = False) -> "WritingResult":
        """Process one event with retry/backoff + dead-letter. Never raises."""
        from .writing_agent import WritingResult

        ref, chash, stype = event.source_ref, event.content_hash, event.source_type

        # Already dead-lettered → don't reprocess a known poison item.
        if self._dl.contains(ref, chash):
            return WritingResult(stype, ref, status="dead_letter",
                                 error="already in dead-letter", acked=False)

        delay = self._cfg.backoff_base
        last_error = "unknown"
        for attempt in range(1, self._cfg.max_retries + 2):  # 1 try + max_retries
            try:
                result = await self._agent.ahandle_event(event, approved=approved)
            except Exception as exc:  # noqa: BLE001 — isolation: agent must never crash the pipeline
                result = WritingResult(stype, ref, status="error",
                                       error=f"unexpected: {exc}", retryable=False)
            result.attempts = attempt

            if result.status != "error":
                return result  # terminal: ingested | skipped | duplicate | pending_approval

            last_error = result.error or "error"
            transient = result.retryable and attempt <= self._cfg.max_retries
            if not transient:
                break
            await asyncio.sleep(delay)
            delay *= self._cfg.backoff_factor

        # Out of retries (or non-retryable) → dead-letter with the reason.
        self._dl.add(source_type=stype, source_ref=ref, content_hash=chash,
                     reason=last_error, attempts=attempt)
        return WritingResult(stype, ref, status="dead_letter", error=last_error,
                             acked=False, attempts=attempt)

    async def process_batch(
        self, events: "list[InboundEvent]", *, approved: bool = False
    ) -> "list[WritingResult]":
        """Process a burst with bounded concurrency; items are isolated, so one
        poison item never blocks the rest. Order of results matches ``events``."""
        sem = asyncio.Semaphore(max(1, self._cfg.concurrency))

        async def one(ev: "InboundEvent") -> "WritingResult":
            async with sem:
                return await self.process_event(ev, approved=approved)

        return await asyncio.gather(*(one(e) for e in events))


# ── On-demand ingest (CLI `ingest-note <file|dir>`) ─────────────────────────


def collect_note_events(targets: "list[Path | str]", connector=None) -> "list[InboundEvent]":
    """Normalize the given file(s)/dir(s) into InboundEvents (no watch loop).

    For a directory, every matching note under it (recursively) is included with a
    ``note:<rel-to-dir>`` ref; for a single file, ``note:<filename>`` relative to
    its parent. Reuses the connector's ``build_event`` (so the same cleaning,
    hashing, and error surfacing apply); unreadable/oversized files are skipped
    (surfaced on the connector), never crashing the backfill."""
    from .connectors.notes import NotesInboxConnector
    from .triggers.connector import ProcessedStore

    events: list = []
    for target in targets:
        path = Path(target).expanduser()
        if path.is_dir():
            root, files = path, [p for p in sorted(path.rglob("*")) if p.is_file()]
        elif path.is_file():
            root, files = path.parent, [path]
        else:
            continue  # missing path: skip (the CLI reports it)
        conn = connector or NotesInboxConnector(
            root, processed_store=ProcessedStore(root / ".mnesis-ondemand.sqlite"), mode="poll",
        )
        # Point a fresh connector at this root so source_refs are relative to it.
        if connector is None:
            conn.inbox = root
        for f in files:
            if f.suffix.lower() not in conn.suffixes:
                continue
            ev = conn.build_event(f)
            if ev is not None:
                events.append(ev)
    return events


async def ingest_note_paths(
    targets: "list[Path | str]",
    *,
    agent: "SourceWritingAgent",
    pipeline: WritingPipeline | None = None,
    approved: bool = False,
) -> "list[WritingResult]":
    """Run the full writing pipeline over the given file(s)/dir(s), immediately.
    The same parse→govern→ingest→ack path as the live connector — for backfills
    and tests. Dedup/retry/dead-letter all apply."""
    pipeline = pipeline or WritingPipeline(agent)
    events = collect_note_events(targets)
    if not events:
        return []
    return await pipeline.process_batch(events, approved=approved)
