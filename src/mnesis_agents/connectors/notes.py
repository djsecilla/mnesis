"""NotesInboxConnector — the first source connector (W1).

Watches a folder for new/changed ``.md`` / ``.txt`` notes and emits one
normalized :class:`InboundEvent` per new item. It is the cleanest instance of the
:class:`SourceConnector` pattern: it *only* detects and normalizes — it never
calls Mnesis or an LLM (that is the WritingAgent's job). Idempotent (a durable
``(source_ref, content_hash)`` ledger), resilient (an unreadable or oversized
file surfaces as an error, never a crash), and works in both poll and watch modes.

Each note becomes an event with a **stable** ``source_ref`` of
``note:<relative-path>`` (so the downstream WritingAgent can ingest it with a
deterministic provenance id, and re-ingesting the same note reinforces rather than
duplicates) and a ``content_hash`` over the file text (so an *edited* note re-emits
while an unchanged one does not).
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from .. import config
from ..triggers.connector import ConnectorError, ProcessedStore, SourceConnector
from ..triggers.events import InboundEvent

#: Default extensions the inbox ingests as text.
DEFAULT_SUFFIXES: frozenset[str] = frozenset({".md", ".txt"})


def _content_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


class NotesInboxConnector(SourceConnector):
    """A folder of Markdown/plain-text notes, surfaced as ``InboundEvent``s."""

    name = "notes"

    def __init__(
        self,
        inbox: Path | str | None = None,
        *,
        processed_store: ProcessedStore | None = None,
        mode: str | None = None,
        poll_interval: float | None = None,
        max_bytes: int | None = None,
        suffixes: frozenset[str] | None = None,
        error_handler=None,
    ) -> None:
        self.inbox = Path(inbox or config.MNESIS_NOTES_INBOX).expanduser()
        self.max_bytes = max_bytes if max_bytes is not None else config.MNESIS_NOTES_MAX_BYTES
        if suffixes is not None:
            self.suffixes = frozenset(s.lower() for s in suffixes)
        else:
            self.suffixes = frozenset(
                s.strip().lower() for s in config.MNESIS_NOTES_SUFFIXES.split(",") if s.strip()
            ) or DEFAULT_SUFFIXES
        store = processed_store or ProcessedStore(
            config.MNESIS_AGENTS_CONNECTOR_STATE_DIR / "notes.sqlite"
        )
        super().__init__(
            name="notes",
            processed_store=store,
            mode=mode or config.MNESIS_NOTES_MODE,
            poll_interval=poll_interval if poll_interval is not None else config.MNESIS_NOTES_POLL_INTERVAL,
            error_handler=error_handler,
        )
        # Watch mode observes the inbox directory.
        self.watch_path = self.inbox
        #: Per-(ref, mtime) guard so a persistently-bad file doesn't re-surface
        #: the same error on every re-scan (a changed file retries).
        self._errored: set[tuple[str, float]] = set()

    def _source_ref(self, path: Path) -> str:
        return f"note:{path.relative_to(self.inbox).as_posix()}"

    async def poll_once(self) -> None:
        """Scan the inbox once and emit an event per new/changed note."""
        if not self.inbox.is_dir():
            return  # nothing to scan yet; not an error
        for path in sorted(self.inbox.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in self.suffixes:
                continue
            await self._handle_file(path)

    async def _handle_file(self, path: Path) -> None:
        event = self.build_event(path)
        if event is not None:
            await self.submit(event)

    def build_event(self, path: Path) -> InboundEvent | None:
        """Normalize one note file into an :class:`InboundEvent`, or ``None`` if it
        is unreadable/oversized (surfaced as an error). Pure normalization — no
        dedup, no submit — so the on-demand path (``ingest-note``) can reuse it."""
        path = Path(path)
        source_ref = self._source_ref(path)
        try:
            stat = path.stat()
        except OSError as exc:
            self._surface_once(source_ref, 0.0, "unreadable", str(exc))
            return None

        if stat.st_size > self.max_bytes:
            self._surface_once(
                source_ref, stat.st_mtime, "oversized",
                f"{stat.st_size} bytes > MNESIS_NOTES_MAX_BYTES ({self.max_bytes})",
            )
            return None

        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            self._surface_once(source_ref, stat.st_mtime, "unreadable", str(exc))
            return None

        return InboundEvent.from_source(
            source_type="notes",
            source_ref=source_ref,
            kind="file_added",
            text=text,
            content_hash=_content_hash(text),
            metadata={
                "path": str(path),
                "rel_path": path.relative_to(self.inbox).as_posix(),
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "suffix": path.suffix.lower(),
            },
        )

    def _surface_once(self, source_ref: str, mtime: float, error: str, detail: str) -> None:
        """Surface a file error at most once per (ref, mtime), so a bad file does
        not spam an error every poll — but an edit (new mtime) retries."""
        key = (source_ref, mtime)
        if key in self._errored:
            return
        self._errored.add(key)
        self.surface_error(ConnectorError(source_ref=source_ref, error=error, detail=detail))
