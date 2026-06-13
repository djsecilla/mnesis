"""Ingest-daemon archetype: long-running, watches a directory, ingests files.

Unlike the interactive archetypes, the daemon does not run an LLM loop — it is
a thin, resilient dispatcher (see ``daemon.py``). Its archetype exists mostly
to carry the allowlist and budgets, keeping the "one core, three profiles"
shape consistent.

It may ingest (mnesis_ingest) and read (mnesis_query / mnesis_get) for dedup.
It never resolves contradictions: routing (auto-resolve high-margin, queue
low-margin) is Mnesis's responsibility.
"""
from __future__ import annotations

from .base import Archetype

#: ingest + read-for-dedup. No resolve/file_back: the daemon never forces
#: contradiction resolution and never files digests.
INGEST_DAEMON_TOOLS = frozenset({
    "mnesis_ingest",
    "mnesis_query",
    "mnesis_get",
})

INGEST_DAEMON_WRITE_ALLOWLIST = frozenset({"mnesis_ingest"})

_SYSTEM = """\
You are the Mnesis ingestion daemon. You ingest new source files into the \
knowledge base as they appear, one at a time, and report the outcome. You do \
not resolve contradictions or supersede pages — Mnesis governs that.\
"""

INGEST_DAEMON = Archetype(
    name="ingest-daemon",
    system_prompt=_SYSTEM,
    tool_allowlist=INGEST_DAEMON_TOOLS,
    write_policy="apply",
    write_allowlist=INGEST_DAEMON_WRITE_ALLOWLIST,
    max_iterations=4,
    max_tool_calls=8,
    max_input_tokens=20_000,
    context_limit=3,
    entry_mode="daemon",
)
