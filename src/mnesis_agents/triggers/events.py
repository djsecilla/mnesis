"""Inbound event triggers (a.k.a. source connectors).

A future email/chat/notes/docs connector implements ``EventTrigger`` and emits a
normalized :class:`InboundEvent`. Only the interface + an in-memory reference
implementation live here — no real connectors.
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class InboundEvent:
    """A normalized inbound event from a source connector.

    The generic runner fields — ``source`` (matched against subscriptions),
    ``kind``, ``payload``, ``metadata``, ``id`` (ack/dedup) — are joined by the
    **normalized source-connector envelope** (W1): ``source_type``, ``source_ref``
    (a stable provenance id, e.g. ``note:<rel-path>``), ``text`` (the extracted
    content), and ``content_hash`` (of the content, for idempotency). For a
    text source ``payload == text``, ``source == source_type``, and
    ``id == source_ref`` — use :meth:`from_source` to fill them consistently.
    """

    source: str                       # connector name, e.g. "email", "notes"
    kind: str                         # event kind, e.g. "message", "file_added"
    payload: Any                      # the raw artifact (string, dict, bytes ref…)
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str | None = None             # stable id for ack/dedup (optional)
    # --- normalized source-connector envelope (W1) ---
    source_type: str | None = None    # the kind of source: "notes", "email", …
    source_ref: str | None = None     # stable provenance id, e.g. "note:<rel-path>"
    text: str | None = None           # the extracted text content
    content_hash: str | None = None   # hash of the content (idempotency key)

    @classmethod
    def from_source(
        cls,
        *,
        source_type: str,
        source_ref: str,
        kind: str,
        text: str,
        content_hash: str,
        metadata: dict[str, Any] | None = None,
    ) -> "InboundEvent":
        """Build a normalized source-connector event, mirroring the envelope onto
        the generic runner fields (``source``/``payload``/``id``) so it works with
        the runner and the default agent input unchanged."""
        return cls(
            source=source_type,
            kind=kind,
            payload=text,
            metadata=metadata or {},
            id=source_ref,
            source_type=source_type,
            source_ref=source_ref,
            text=text,
            content_hash=content_hash,
        )


class EventTrigger(ABC):
    """A source of inbound events. Connectors implement this interface.

    ``stream()`` yields normalized events; ``ack()`` is an optional idempotency
    hook the runner calls after a successful dispatch.
    """

    #: Connector name (used in run records / event matching).
    name: str = "events"

    @abstractmethod
    def stream(self) -> AsyncIterator[InboundEvent]:
        """Yield inbound events as they arrive (an async iterator)."""

    async def ack(self, event: InboundEvent) -> None:
        """Mark an event processed. No-op by default; connectors may override."""
        return None


class InMemoryEventTrigger(EventTrigger):
    """An in-memory queue-backed event source for tests and local wiring.

    Push events with ``emit``; the runner consumes them via ``stream``. Acked
    events are recorded so tests can assert the idempotency hook fired.
    """

    def __init__(self, name: str = "memory") -> None:
        self.name = name
        self._queue: asyncio.Queue[InboundEvent] = asyncio.Queue()
        self.acked: list[InboundEvent] = []

    async def emit(self, event: InboundEvent) -> None:
        await self._queue.put(event)

    async def stream(self) -> AsyncIterator[InboundEvent]:
        while True:  # cancelled by the runner on stop
            yield await self._queue.get()

    async def ack(self, event: InboundEvent) -> None:
        self.acked.append(event)
