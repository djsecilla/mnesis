"""The SourceConnector pattern — THE contract every inbound source implements.

A *source connector* turns some external feed (a notes folder, an email inbox, a
chat channel, a docs store) into a stream of normalized :class:`InboundEvent`s.
It does **one job**: *detect and normalize*. It never calls Mnesis or an LLM —
that is the WritingAgent's job downstream. The connector only surfaces *what
arrived*; the agent decides *what to do with it*.

Every future source implements this same shape, so the runtime treats them
uniformly:

  - **Lifecycle** — ``start()`` begins detection (watch or poll), ``stop()`` halts
    it cleanly. ``stream()`` (the F5 ``EventTrigger`` interface) yields events as
    they are detected, so a connector is a drop-in event trigger for the runner.
  - **Idempotency** — every emitted item is recorded in a durable
    :class:`ProcessedStore` keyed by ``(source_ref, content_hash)``. Re-seeing the
    same content is a no-op; changed content (a new hash for the same ref)
    re-emits. ``ack()`` marks an event fully processed after a successful dispatch.
  - **Resilience** — a bad item (unreadable, oversized, malformed) is surfaced as a
    :class:`ConnectorError` (in ``self.errors`` and to an optional handler) and the
    detection loop keeps running. One bad item never stops the watch.

A subclass implements exactly one method, :meth:`poll_once` — a single detection
pass that scans the source, builds an :class:`InboundEvent` per new item, and
calls :meth:`submit`. Both poll and watch modes reuse it: poll calls it on a
timer; watch calls it on every filesystem/source change (a re-scan, so no event
is ever missed to a race). The base provides everything else.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from abc import abstractmethod
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .events import EventTrigger, InboundEvent

log = logging.getLogger("mnesis_agents.connector")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Processed-state store (durable idempotency ledger) ──────────────────────


class ProcessedStore:
    """A small durable ledger of ``(source_ref, content_hash)`` a connector has
    emitted/processed — so detection is idempotent across re-scans and restarts.

    Presence of a row means "already seen"; the ``status`` records how far it got
    (``emitted`` → on the queue; ``processed`` → acked after dispatch). SQLite for
    concurrency-safety, mirroring the core's state store.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS processed (
                source_ref   TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'emitted',
                first_seen   TEXT NOT NULL,
                updated      TEXT NOT NULL,
                PRIMARY KEY (source_ref, content_hash)
            )
            """
        )
        return conn

    def seen(self, source_ref: str, content_hash: str) -> bool:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM processed WHERE source_ref = ? AND content_hash = ?",
                (source_ref, content_hash),
            ).fetchone()
        finally:
            conn.close()
        return row is not None

    def status(self, source_ref: str, content_hash: str) -> str | None:
        """The recorded status (``emitted``/``processed``) for an item, or ``None``
        if unseen. Lets a consumer skip an item that was already *processed* (acked)
        rather than merely emitted."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT status FROM processed WHERE source_ref = ? AND content_hash = ?",
                (source_ref, content_hash),
            ).fetchone()
        finally:
            conn.close()
        return row["status"] if row is not None else None

    def record(self, source_ref: str, content_hash: str, status: str = "emitted") -> None:
        conn = self._connect()
        try:
            now = _now()
            conn.execute(
                """
                INSERT INTO processed (source_ref, content_hash, status, first_seen, updated)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source_ref, content_hash) DO UPDATE SET
                    status = excluded.status, updated = excluded.updated
                """,
                (source_ref, content_hash, status, now, now),
            )
            conn.commit()
        finally:
            conn.close()

    def mark_processed(self, source_ref: str, content_hash: str) -> None:
        """Mark a previously-emitted item fully processed (after a successful ack)."""
        if source_ref and content_hash:
            self.record(source_ref, content_hash, "processed")

    def all(self) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT source_ref, content_hash, status, first_seen, updated "
                "FROM processed ORDER BY first_seen"
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]


# ── Error surfacing ─────────────────────────────────────────────────────────


@dataclass
class ConnectorError:
    """A surfaced (not raised) detection error — a bad item, never a crash."""

    source_ref: str | None
    error: str                     # short category, e.g. "unreadable", "oversized"
    detail: str = ""
    ts: str = field(default_factory=_now)


# ── The connector base ──────────────────────────────────────────────────────

_SENTINEL = object()


class SourceConnector(EventTrigger):
    """Base class for inbound source connectors (see the module docstring).

    Subclasses set ``name`` and (for watch mode) ``watch_path``, and implement
    :meth:`poll_once`. Everything else — lifecycle, the queue, idempotent
    :meth:`submit`, error surfacing, and the poll/watch loops — is provided here.
    """

    #: Detection mode. "poll" = timed re-scans (no extra dependency); "watch" =
    #: filesystem events via watchdog, falling back to poll if it isn't installed.
    mode: str = "poll"
    poll_interval: float = 2.0
    #: Filesystem path watch mode observes (subclasses that support watch set it).
    watch_path: Path | None = None

    def __init__(
        self,
        *,
        name: str | None = None,
        processed_store: ProcessedStore,
        mode: str | None = None,
        poll_interval: float | None = None,
        error_handler: Callable[[ConnectorError], None] | None = None,
    ) -> None:
        if name:
            self.name = name
        if mode:
            self.mode = mode
        if poll_interval is not None:
            self.poll_interval = poll_interval
        self._store = processed_store
        self._error_handler = error_handler
        self.errors: list[ConnectorError] = []
        self._queue: asyncio.Queue = asyncio.Queue()
        self._detect_task: asyncio.Task | None = None
        self._stopped = asyncio.Event()
        self._rescan = asyncio.Event()
        self._observer = None  # watchdog Observer when in watch mode

    # -- lifecycle -------------------------------------------------------------

    async def start(self) -> None:
        """Begin detection (idempotent — calling twice is a no-op)."""
        if self._detect_task is None or self._detect_task.done():
            # Fresh session: a clean queue so a leftover stop-sentinel from a prior
            # start/stop cycle can't poison this one.
            self._queue = asyncio.Queue()
            self._rescan = asyncio.Event()
            self._stopped.clear()
            self._detect_task = asyncio.create_task(self._run_detection())

    async def stop(self) -> None:
        """Halt detection and unblock any in-flight ``stream()``."""
        self._stopped.set()
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=2)
            except Exception:  # noqa: BLE001
                pass
            self._observer = None
        if self._detect_task is not None:
            self._detect_task.cancel()
            try:
                await self._detect_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._detect_task = None
        await self._queue.put(_SENTINEL)

    async def stream(self) -> AsyncIterator[InboundEvent]:
        """Yield detected events (F5 ``EventTrigger``). Auto-starts detection."""
        await self.start()
        while not self._stopped.is_set():
            item = await self._queue.get()
            if item is _SENTINEL:
                break
            yield item

    async def ack(self, event: InboundEvent) -> None:
        """Mark an event fully processed (idempotency hook the runner calls)."""
        if event.source_ref and event.content_hash:
            self._store.mark_processed(event.source_ref, event.content_hash)

    # -- emit + error surfacing (used by subclasses' poll_once) ----------------

    async def submit(self, event: InboundEvent) -> bool:
        """Emit ``event`` unless its ``(source_ref, content_hash)`` was already
        seen. Returns True if emitted, False if deduplicated. The dedup is
        recorded *before* queueing, so a concurrent re-scan never double-emits."""
        ref, chash = event.source_ref, event.content_hash
        if not ref or not chash:
            log.warning("connector %s: dropping event with no source_ref/content_hash", self.name)
            return False
        if self._store.seen(ref, chash):
            return False
        self._store.record(ref, chash, "emitted")
        await self._queue.put(event)
        return True

    def surface_error(self, error: ConnectorError) -> None:
        """Record a detection error (never raises). Keeps the loop alive."""
        self.errors.append(error)
        log.warning("connector %s error [%s] %s: %s",
                    self.name, error.error, error.source_ref, error.detail)
        if self._error_handler is not None:
            try:
                self._error_handler(error)
            except Exception:  # noqa: BLE001 — a bad handler never breaks detection
                log.exception("connector %s error_handler raised", self.name)

    # -- detection loops (poll / watch) ----------------------------------------

    async def _run_detection(self) -> None:
        # Initial scan in both modes, so items already present are picked up.
        await self._safe_poll()
        if self.mode == "watch":
            await self._run_watch()
        else:
            await self._run_poll()

    async def _safe_poll(self) -> None:
        """Run one detection pass, turning any unexpected error into a surfaced
        error rather than killing the loop."""
        try:
            await self.poll_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.surface_error(ConnectorError(None, "scan_failed", str(exc)))

    async def _run_poll(self) -> None:
        while not self._stopped.is_set():
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self.poll_interval)
                return  # stop requested
            except asyncio.TimeoutError:
                pass  # interval elapsed -> rescan
            await self._safe_poll()

    async def _run_watch(self) -> None:
        observer = self._build_observer()
        if observer is None:  # watchdog unavailable -> degrade to poll
            log.warning("connector %s: watchdog unavailable, falling back to poll mode", self.name)
            await self._run_poll()
            return
        self._observer = observer
        observer.start()
        try:
            while not self._stopped.is_set():
                # Wake on a filesystem change OR periodically (a safety re-scan).
                try:
                    await asyncio.wait_for(self._rescan.wait(), timeout=self.poll_interval)
                except asyncio.TimeoutError:
                    pass
                self._rescan.clear()
                if self._stopped.is_set():
                    break
                await self._safe_poll()
        finally:
            try:
                observer.stop()
                observer.join(timeout=2)
            except Exception:  # noqa: BLE001
                pass
            self._observer = None

    def _build_observer(self):
        """A watchdog Observer that signals a re-scan on any change under
        ``watch_path``. Returns None if watchdog isn't installed or no path is set."""
        if self.watch_path is None:
            return None
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:
            return None

        loop = asyncio.get_running_loop()
        rescan = self._rescan

        class _Handler(FileSystemEventHandler):
            def on_any_event(self, event):  # noqa: ANN001, ARG002
                loop.call_soon_threadsafe(rescan.set)

        observer = Observer()
        self.watch_path.mkdir(parents=True, exist_ok=True)
        observer.schedule(_Handler(), str(self.watch_path), recursive=True)
        return observer

    # -- the one thing a subclass implements -----------------------------------

    @abstractmethod
    async def poll_once(self) -> None:
        """One detection pass: scan the source and :meth:`submit` an
        :class:`InboundEvent` for each new item; surface bad items via
        :meth:`surface_error`. Must not raise for a single bad item."""
