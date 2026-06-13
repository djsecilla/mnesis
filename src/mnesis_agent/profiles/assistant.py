"""Assistant archetype: interactive, read-only, propose-only write policy.

A grounded Q&A companion.  It answers from retrieved Mnesis pages with
citations and, when it has produced a durable answer, *proposes* filing it
back as a digest — but never writes on its own.  The human confirms; only then
does the CLI call ``mnesis_file_back``.
"""
from __future__ import annotations

from .base import Archetype

#: Read-only graph + page tools. No write tool is in the allowlist, so the
#: model cannot file anything back itself — write-back is human-confirmed.
ASSISTANT_TOOLS = frozenset({
    "mnesis_query",
    "mnesis_get",
    "mnesis_entity",
    "mnesis_impact",
    "mnesis_traverse",
})

_SYSTEM = """\
You are the Mnesis assistant: a knowledgeable companion that answers strictly \
from the project's knowledge base.

- Answer using the pre-loaded context and the read tools (mnesis_query, \
mnesis_get, mnesis_entity, mnesis_impact, mnesis_traverse).
- If the knowledge base does not support an answer, say so plainly rather than \
speculating.
- Be concise and direct. Prefer a clear, declarative answer over hedging.\
"""

ASSISTANT = Archetype(
    name="assistant",
    system_prompt=_SYSTEM,
    tool_allowlist=ASSISTANT_TOOLS,
    write_policy="propose",          # proposes a digest; never writes itself
    write_allowlist=None,
    max_iterations=6,
    max_tool_calls=18,
    max_input_tokens=40_000,
    context_limit=5,
    entry_mode="interactive",
)
