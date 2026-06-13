"""Research archetype: bounded multi-step investigation, digests-only apply.

Runs a deeper, budget-bounded investigation (query → traverse → impact →
synthesize), produces a cited report, and crystallizes a single digest back
into Mnesis via ``mnesis_file_back``.

Write policy is ``apply`` but the write allowlist is *only* ``mnesis_file_back``:
research creates digests, it never ingests raw sources and never supersedes.
Contradiction/supersession remain Mnesis's job.
"""
from __future__ import annotations

from .base import Archetype

#: All read/graph tools plus file_back. Note: mnesis_ingest and mnesis_resolve
#: are deliberately absent — research crystallizes digests only, never writes
#: raw sources and never forces a supersession.
RESEARCH_TOOLS = frozenset({
    "mnesis_query",
    "mnesis_get",
    "mnesis_entity",
    "mnesis_impact",
    "mnesis_neighbors",
    "mnesis_traverse",
    "mnesis_file_back",
})

#: Only file_back counts as a write (digests only).
RESEARCH_WRITE_ALLOWLIST = frozenset({"mnesis_file_back"})

_SYSTEM = """\
You are the Mnesis research agent: a rigorous investigator that builds a \
well-grounded answer to a goal by exploring the knowledge base over several \
steps.

- Investigate methodically: search (mnesis_query), follow the graph \
(mnesis_traverse, mnesis_neighbors, mnesis_entity), and assess blast radius \
(mnesis_impact) before concluding.
- Ground every claim in retrieved pages and cite their ids as [page-id].
- When your investigation is complete, synthesize a clear, cited report.
- Then crystallize the result by calling mnesis_file_back exactly once with \
the original goal as the question and your report as the answer, so future \
queries benefit. Do not file more than one digest, and never ingest raw \
sources or resolve contradictions — that is not your role.\
"""

RESEARCH = Archetype(
    name="research",
    system_prompt=_SYSTEM,
    tool_allowlist=RESEARCH_TOOLS,
    write_policy="apply",
    write_allowlist=RESEARCH_WRITE_ALLOWLIST,   # digests only
    max_iterations=12,
    max_tool_calls=40,
    max_input_tokens=80_000,
    context_limit=8,
    entry_mode="batch",
)
