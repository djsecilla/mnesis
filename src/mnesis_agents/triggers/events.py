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

    ``id`` (when a connector sets it) lets the runner ack / de-duplicate so a
    connector can mark events processed (idempotency-friendly).
    """

    source: str                       # connector name, e.g. "email", "notes"
    kind: str                         # event kind, e.g. "message", "file_added"
    payload: Any                      # the raw artifact (string, dict, bytes ref…)
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str | None = None             # stable id for ack/dedup (optional)


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


#: Source connectors implement exactly the EventTrigger interface.
SourceConnector = EventTrigger


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
