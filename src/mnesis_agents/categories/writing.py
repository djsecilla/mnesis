"""WritingAgent — the ingest category.

Trigger: an inbound **source event** (a new document/message/file). The agent
parses the input artifact and ingests it into Mnesis via ``mnesis_ingest``;
Mnesis governs redaction and contradiction/supersession server-side, so the
write policy is simply ``ingest`` (the agent decides *whether* to call ingest,
never *how* the write is made safe).
"""
from __future__ import annotations

from abc import abstractmethod
from typing import Any, ClassVar

from .base import CategoryAgent


class WritingAgent(CategoryAgent):
    """Abstract base for source-ingesting agents.

    Concrete subclasses must declare the **input shape** by implementing
    ``parse_artifact`` (inbound event -> the text to ingest) and ``source_ref``
    (a stable provenance id). They reach Mnesis only through ``mnesis_ingest``.
    """

    category: ClassVar[str] = "writing"
    trigger: ClassVar[str] = "event"        # inbound source event
    write_policy: ClassVar[str] = "ingest"

    #: A human-readable description of the inbound artifact this agent accepts.
    input_shape: ClassVar[str] = "an inbound source artifact"

    def write_tools(self) -> frozenset[str]:
        return frozenset({"mnesis_ingest"})

    @abstractmethod
    def parse_artifact(self, event: Any) -> str:
        """Turn an inbound source event into the redacted-on-server text to ingest."""

    @abstractmethod
    def source_ref(self, event: Any) -> str:
        """A stable provenance id for the event (used as mnesis_ingest source_ref)."""
