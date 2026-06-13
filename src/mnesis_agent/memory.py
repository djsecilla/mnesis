"""Mnesis memory integration: context loading, grounding, and crystallization.

Wraps the core run_agent loop (A3) with three memory behaviours:

  1. Session-start context load — mnesis_query on the user goal, injected
     into the system prompt before the first LLM call, so the agent starts
     already grounded in relevant pages.

  2. Grounding + citation convention — the system prompt instructs the agent
     to cite page IDs it draws on; the loop's citation extractor (A3) maps
     those back to real tool results, never to invented IDs.

  3. Crystallization (write-back) — two modes governed by write_policy:
       "propose"  the agent writes nothing; a DigestProposal{question,
                  answer, citations} is synthesised from the final answer and
                  returned in GroundedAgentResult for the caller to review.
       "apply"    write tools (mnesis_file_back / mnesis_ingest) are included
                  in the loop's tool list; per-write governance (redaction,
                  contradiction detection) is enforced server-side by Mnesis.
       "off"      read-only; no proposal, no writes.

All Mnesis access goes through the MCP tool registry — never via Mnesis
internals.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable, Literal

from .loop import AgentProfile, AgentResult, run_agent
from .mcp_client import ToolSpec
from .provider import Provider
from .registry import ToolRegistry


# ── Write policy ──────────────────────────────────────────────────────────────

WritePolicy = Literal["off", "propose", "apply"]

#: Default set of tool names considered writes (enforced in propose/off modes).
DEFAULT_WRITE_TOOLS: frozenset[str] = frozenset({"mnesis_file_back", "mnesis_ingest"})


# ── Types ─────────────────────────────────────────────────────────────────────


@dataclass
class DigestProposal:
    """A proposed write-back to Mnesis.

    Returned in propose mode so the caller can inspect and optionally file it;
    nothing is written automatically.
    """

    question: str
    answer: str
    citations: list[str]  # page IDs grounding the answer (from tool results)


@dataclass
class MemoryProfile:
    """Unified configuration for a grounded agent run.

    Combines the base system prompt, loop budgets, and memory policy so
    callers only need one object.
    """

    base_system: str

    # ── Loop budgets (forwarded to AgentProfile) ──────────────────────────
    max_iterations: int = 10
    max_tool_calls: int = 30
    max_input_tokens: int = 50_000
    timeout_seconds: float | None = None

    # ── Memory configuration ───────────────────────────────────────────────
    write_policy: WritePolicy = "off"
    #: Tool names allowed to write in apply mode (None → DEFAULT_WRITE_TOOLS).
    write_allowlist: frozenset[str] | None = None
    #: How many context hits to pre-load at session start.
    context_limit: int = 5


@dataclass
class GroundedAgentResult(AgentResult):
    """AgentResult extended with memory-specific fields.

    Inherits the full core result (final_text, transcript, citations, writes,
    stop_reason, usage, iterations) and adds:

    ``context_loaded``   the pre-loaded context string injected into the system
                         prompt (empty if context load failed or returned nothing).
    ``proposal``         populated in propose mode; None in apply/off mode.
    """

    context_loaded: str = ""
    proposal: DigestProposal | None = None


# ── Context loading ───────────────────────────────────────────────────────────

_CONTEXT_HEADER = "## Knowledge base context (pre-loaded for this query)"


async def load_context(
    goal: str,
    registry: ToolRegistry,
    *,
    limit: int = 5,
) -> str:
    """Query the knowledge base for the user's goal and format results.

    Returns a Markdown string ready to inject into the system prompt.
    Returns ``""`` on tool failure or empty results (graceful degradation —
    the agent can still proceed using its tool calls).
    """
    try:
        raw = await registry.dispatch("mnesis_query", {"query": goal, "limit": limit})
        data = json.loads(raw)
    except Exception:
        return ""

    hits = data.get("hits", [])
    if not hits:
        return ""

    lines: list[str] = [_CONTEXT_HEADER]
    for h in hits:
        pid = h.get("id", "")
        title = h.get("title", "")
        snippet = h.get("snippet", "")
        conf = h.get("confidence", 0.0)
        lines.append(f"\n### [{pid}] {title}")
        if snippet:
            lines.append(f"> {snippet}")
        lines.append(f"  confidence: {conf:.2f}")

    return "\n".join(lines)


# ── System prompt ─────────────────────────────────────────────────────────────

_CITATION_INSTRUCTION = (
    "When you answer, cite the knowledge-base pages you draw on using the format "
    "[page-id] (e.g. 'Project Atlas uses Redis [atlas]'). "
    "Only cite pages that were actually returned by tool calls or appear in the "
    "pre-loaded context below — never invent page identifiers."
)

_PROPOSE_NOTE = (
    "Do NOT call any write tools (mnesis_ingest, mnesis_file_back). "
    "A digest proposal will be generated from your final answer automatically."
)

_APPLY_NOTE = (
    "When you have synthesised a high-quality, grounded answer, you may call "
    "mnesis_file_back to crystallise it as a digest page so future queries benefit."
)


def make_grounded_system(
    base: str,
    context: str,
    write_policy: WritePolicy = "off",
) -> str:
    """Build a system prompt that grounds the agent in retrieved Mnesis pages.

    Appends:
      - Citation convention (always).
      - A write-policy note (propose or apply modes only).
      - The pre-loaded context block (when non-empty).
    """
    parts: list[str] = [base.rstrip(), "", _CITATION_INSTRUCTION]

    if write_policy == "propose":
        parts.append(_PROPOSE_NOTE)
    elif write_policy == "apply":
        parts.append(_APPLY_NOTE)

    if context:
        parts.extend(["", context])

    return "\n".join(parts)


# ── Main entry point ──────────────────────────────────────────────────────────


async def run_grounded_agent(
    profile: MemoryProfile,
    user_input: str,
    tools: list[ToolSpec],
    provider: Provider,
    registry: ToolRegistry,
    *,
    audit_hook: Callable[[dict], None] | None = None,
) -> GroundedAgentResult:
    """Run the bounded agent loop with Mnesis memory integration.

    Steps
    -----
    1. Load session-start context: mnesis_query on ``user_input`` injects the
       top ``context_limit`` pages into the system prompt before the first LLM
       call so the agent is already grounded.
    2. Build the grounded system prompt (base + citation convention + context).
    3. Determine the active write tools:
       - apply  → write_allowlist (or DEFAULT_WRITE_TOOLS) are tracked in
                  result.writes when the model calls them.
       - propose / off → write tools are removed from the tools list so the
                  model never sees them; loop_write_tool_names is None.
    4. Run the core loop via run_agent (A3).
    5. In propose mode, synthesise a DigestProposal from the final answer and
       citations — nothing is written.
    """
    effective_write_tools: frozenset[str] = (
        profile.write_allowlist
        if profile.write_allowlist is not None
        else DEFAULT_WRITE_TOOLS
    )

    # ── 1. Session-start context load ────────────────────────────────────────
    context = await load_context(user_input, registry, limit=profile.context_limit)

    # ── 2. Grounded system prompt ─────────────────────────────────────────────
    system = make_grounded_system(profile.base_system, context, profile.write_policy)

    # ── 3. Tool governance based on write policy ──────────────────────────────
    if profile.write_policy == "apply":
        # Write tools are visible to the model and tracked as writes.
        active_tools = list(tools)
        loop_write_tool_names: set[str] | None = set(effective_write_tools)
    else:
        # propose / off: hide write tools from the model so it cannot call them.
        active_tools = [t for t in tools if t.name not in effective_write_tools]
        loop_write_tool_names = None

    # ── 4. Core loop ──────────────────────────────────────────────────────────
    loop_profile = AgentProfile(
        system=system,
        max_iterations=profile.max_iterations,
        max_tool_calls=profile.max_tool_calls,
        max_input_tokens=profile.max_input_tokens,
        timeout_seconds=profile.timeout_seconds,
    )
    core: AgentResult = await run_agent(
        loop_profile,
        user_input,
        active_tools,
        provider,
        registry,
        write_tool_names=loop_write_tool_names,
        audit_hook=audit_hook,
    )

    # ── 5. Crystallization in propose mode ────────────────────────────────────
    proposal: DigestProposal | None = None
    if profile.write_policy == "propose" and core.final_text:
        proposal = DigestProposal(
            question=user_input,
            answer=core.final_text,
            citations=list(core.citations),
        )

    return GroundedAgentResult(
        # AgentResult fields
        final_text=core.final_text,
        transcript=core.transcript,
        tools_used=core.tools_used,
        citations=core.citations,
        writes=core.writes,
        stop_reason=core.stop_reason,
        usage=core.usage,
        iterations=core.iterations,
        # Memory fields
        context_loaded=context,
        proposal=proposal,
    )
