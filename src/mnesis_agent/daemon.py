"""Ingestion daemon: watch a directory and ingest new files into Mnesis.

The daemon is a thin, resilient dispatcher rather than an LLM loop. For each
new file it reads the (text) content and calls ``mnesis_ingest`` once, then
logs the outcome. The heavy lifting — extraction, dedup against existing
pages, and contradiction/supersession routing — is Mnesis's job server-side;
the daemon never forces a resolution.

Design guarantees:
  * **Resilient**  — one bad file (unreadable, empty, or a tool error) is
    logged and skipped; the watch loop keeps running.
  * **Idempotent** — each file maps to a stable ``source_ref``; once ingested
    it is remembered and never re-dispatched, so re-seeing it is a no-op.
  * **Non-coercive** — a ``contradict`` outcome is logged with its review id;
    the daemon does not call mnesis_resolve.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from .registry import ToolRegistry

logger = logging.getLogger("mnesis_agent.daemon")

#: File extensions the daemon will attempt to ingest as text.
DEFAULT_SUFFIXES: frozenset[str] = frozenset({".txt", ".md", ".markdown"})


# ── Outcome ───────────────────────────────────────────────────────────────────


@dataclass
class IngestOutcome:
    """The result of attempting to ingest one file.

    ``status``:
      "ingested"           dispatched to mnesis_ingest; see ``action``
      "skipped_duplicate"  already ingested in this daemon's lifetime
      "skipped_malformed"  unreadable / empty — not dispatched
      "error"              tool dispatch raised; not marked seen (may retry)

    ``action`` mirrors Mnesis's server-side routing for an ingested file:
    "new" | "reinforce" | "supersede" | "contradict" (None unless ingested).
    """

    path: str
    source_ref: str
    status: str
    action: str | None = None
    page_id: str | None = None
    review_id: int | None = None
    message: str = ""


def _parse_ingest_result(raw: str) -> dict:
    """Extract {action, page_id, review_id} from a mnesis_ingest result.

    Tolerant of two shapes: a JSON ``IngestResult`` (the in-process FakeToolSource
    and any structured server) and the real MCP tool's human-readable ``key:
    value`` text (``ingested page: <id>`` / ``action: <a>`` / ``review: <n>``).
    Missing fields come back as None so the daemon degrades gracefully.
    """
    out: dict = {"action": None, "page_id": None, "review_id": None}
    raw = (raw or "").strip()
    if not raw:
        return out

    # JSON first.
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        data = None
    if isinstance(data, dict):
        out["action"] = data.get("action_taken") or data.get("action")
        out["page_id"] = data.get("page_id") or data.get("id")
        out["review_id"] = data.get("review_id")
        return out

    # Text fallback: parse "key: value" lines.
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key in ("ingested page", "page", "page_id") and not out["page_id"]:
            out["page_id"] = value
        elif key == "action":
            out["action"] = value
        elif key == "review":
            try:
                out["review_id"] = int(value)
            except ValueError:
                pass
    return out


def _slug(text: str) -> str:
    """Collision-light slug for a source_ref derived from a filename."""
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or "source"


def source_ref_for(path: Path) -> str:
    """Derive a stable source_ref from a file path (its slugified stem)."""
    return _slug(path.stem)


# ── Daemon ────────────────────────────────────────────────────────────────────


class IngestDaemon:
    """Watches a directory and ingests new text files into Mnesis.

    Parameters
    ----------
    registry:
        Tool registry whose dispatch routes to the Mnesis MCP endpoint.
    suffixes:
        File extensions to consider (default: .txt / .md / .markdown).
    max_bytes:
        Files larger than this are skipped as malformed (guards against huge
        or binary files).
    """

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        suffixes: frozenset[str] = DEFAULT_SUFFIXES,
        max_bytes: int = 2_000_000,
    ) -> None:
        self._registry = registry
        self._suffixes = suffixes
        self._max_bytes = max_bytes
        self._seen: set[str] = set()  # source_refs already ingested (idempotency)

    @property
    def seen(self) -> set[str]:
        """Source_refs ingested so far (read-only view for inspection/tests)."""
        return set(self._seen)

    def _read_text(self, path: Path) -> str | None:
        """Read a file as UTF-8 text. Returns None if unreadable/empty/too big."""
        try:
            if path.stat().st_size > self._max_bytes:
                return None
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        return text if text.strip() else None

    async def process_file(self, path: str | Path) -> IngestOutcome:
        """Ingest one file (idempotently). Never raises — always returns an outcome."""
        path = Path(path)
        source_ref = source_ref_for(path)

        # Idempotency: a file ingested once is never dispatched again.
        if source_ref in self._seen:
            logger.info("skip duplicate: %s (source_ref=%s)", path, source_ref)
            return IngestOutcome(str(path), source_ref, "skipped_duplicate")

        # Malformed / unreadable: log, mark seen (don't retry a bad file), skip.
        text = self._read_text(path)
        if text is None:
            self._seen.add(source_ref)
            logger.warning("skip malformed/unreadable: %s", path)
            return IngestOutcome(str(path), source_ref, "skipped_malformed",
                                 message="unreadable, empty, or too large")

        # Dispatch the ingest. A tool error is logged and skipped (NOT marked
        # seen, so a transient failure can be retried on the next scan).
        try:
            raw = await self._registry.dispatch(
                "mnesis_ingest", {"text": text, "source_ref": source_ref}
            )
        except Exception as exc:  # noqa: BLE001 — resilience: never kill the loop
            logger.error("ingest error for %s: %s", path, exc)
            return IngestOutcome(str(path), source_ref, "error", message=str(exc))

        # Parse the server outcome. The daemon only reports routing — it never
        # acts on a contradiction (no mnesis_resolve call).
        parsed = _parse_ingest_result(raw)
        action = parsed["action"]
        page_id = parsed["page_id"]
        review_id = parsed["review_id"]

        self._seen.add(source_ref)
        if action == "contradict":
            logger.info(
                "ingested %s -> contradiction queued for review (review_id=%s); "
                "leaving resolution to Mnesis", path, review_id,
            )
        else:
            logger.info("ingested %s -> action=%s page_id=%s", path, action, page_id)

        return IngestOutcome(
            str(path), source_ref, "ingested",
            action=action, page_id=page_id, review_id=review_id,
        )

    def _candidate_files(self, directory: Path) -> list[Path]:
        """New, eligible files in the directory, sorted for deterministic order."""
        if not directory.is_dir():
            return []
        out: list[Path] = []
        for p in sorted(directory.iterdir()):
            if p.is_file() and p.suffix.lower() in self._suffixes:
                if source_ref_for(p) not in self._seen:
                    out.append(p)
        return out

    async def scan_once(self, directory: str | Path) -> list[IngestOutcome]:
        """Process every new eligible file in the directory once.

        Resilient: each file is processed independently; process_file never
        raises, so one bad file cannot abort the scan.
        """
        directory = Path(directory)
        outcomes: list[IngestOutcome] = []
        for path in self._candidate_files(directory):
            outcomes.append(await self.process_file(path))
        return outcomes

    async def watch(
        self,
        directory: str | Path,
        *,
        poll_interval: float = 2.0,
        max_cycles: int | None = None,
        should_stop: Callable[[], bool] | None = None,
        on_outcome: Callable[[IngestOutcome], None] | None = None,
    ) -> list[IngestOutcome]:
        """Poll the directory and ingest new files until stopped.

        Bounded for testability: ``max_cycles`` caps the number of scan cycles,
        ``should_stop`` lets a caller break out early. Returns all outcomes
        produced across cycles.

        Long-running by default (``max_cycles=None``, runs until should_stop).
        """
        import asyncio

        all_outcomes: list[IngestOutcome] = []
        cycle = 0
        logger.info("watching %s (poll=%.1fs)", directory, poll_interval)
        while True:
            if should_stop is not None and should_stop():
                break
            if max_cycles is not None and cycle >= max_cycles:
                break

            for outcome in await self.scan_once(directory):
                all_outcomes.append(outcome)
                if on_outcome is not None:
                    on_outcome(outcome)

            cycle += 1
            if max_cycles is not None and cycle >= max_cycles:
                break
            if should_stop is not None and should_stop():
                break
            await asyncio.sleep(poll_interval)

        return all_outcomes
