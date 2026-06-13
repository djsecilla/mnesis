"""The three Mnesis agent archetypes, as profiles over one core.

Each archetype bundles a system prompt, a tool allowlist, a write policy,
budgets, and an entry mode. They share the loop (A3) and memory behaviours
(A4); only the profile differs.
"""
from __future__ import annotations

from .assistant import ASSISTANT
from .base import Archetype, EntryMode, filter_to_allowlist, to_memory_profile
from .ingest_daemon import INGEST_DAEMON
from .research import RESEARCH

#: Lookup by CLI name.
ARCHETYPES: dict[str, Archetype] = {
    ASSISTANT.name: ASSISTANT,
    RESEARCH.name: RESEARCH,
    INGEST_DAEMON.name: INGEST_DAEMON,
}


def get_archetype(name: str) -> Archetype:
    """Return the archetype by name, or raise KeyError with the valid names."""
    try:
        return ARCHETYPES[name]
    except KeyError:
        raise KeyError(
            f"Unknown archetype {name!r}. Valid: {sorted(ARCHETYPES)}"
        ) from None


__all__ = [
    "Archetype",
    "EntryMode",
    "ASSISTANT",
    "RESEARCH",
    "INGEST_DAEMON",
    "ARCHETYPES",
    "get_archetype",
    "to_memory_profile",
    "filter_to_allowlist",
]
