"""Shared run plumbing: build a registry, pick a provider, run an archetype.

This is the seam between the archetypes (A5) and the loop/memory core (A3/A4),
and where the safety story (A6) is assembled: policy enforcement, the run
audit, and the opt-in local-tool registry all come together in run_archetype.
"""
from __future__ import annotations

import json
from typing import Callable

from . import config
from .audit import AuditLog
from .local_tools import LocalToolSource, build_local_tool_source
from .loop import ToolStep
from .mcp_client import MCPToolSource, ToolSource
from .memory import GroundedAgentResult, run_grounded_agent
from .policy import PolicyEnforcingRegistry, ToolPolicy
from .profiles import Archetype, filter_to_allowlist, to_memory_profile
from .provider import Provider, get_provider
from .registry import ToolRegistry


def build_registry(
    sources: list[ToolSource] | None = None,
    *,
    local_tools: LocalToolSource | None = None,
) -> ToolRegistry:
    """Build the tool registry.

    With no ``sources``, connects to the configured Mnesis MCP endpoint. Pass
    explicit sources (e.g. a FakeToolSource, or Mnesis + local tools) to
    override — that is how tests and multi-source setups wire it.

    ``local_tools`` (when given) is added as an additional source. By default
    it is OFF — ``build_local_tool_source()`` returns None unless the opt-in
    flag is set, so a plain run starts with only the Mnesis tools.
    """
    registry = ToolRegistry()
    if sources is None:
        registry.add_source(MCPToolSource(config.MNESIS_MCP_URL, config.MNESIS_MCP_TOKEN))
    else:
        for src in sources:
            registry.add_source(src)
    if local_tools is not None:
        registry.add_source(local_tools)
    return registry


async def run_archetype(
    arch: Archetype,
    user_input: str,
    registry: ToolRegistry,
    provider: Provider | None = None,
    *,
    audit: AuditLog | None = None,
    local_tool_names: frozenset[str] = frozenset(),
    audit_hook: Callable[[dict], None] | None = None,
) -> GroundedAgentResult:
    """Run one archetype against the registry, with policy + audit enforced.

    Enforcement points assembled here:

    1. **Allowlist / write policy** — a ToolPolicy is built from the archetype
       and wrapped around the registry as a PolicyEnforcingRegistry. Every
       dispatch is checked *before* any side effect; a refusal is surfaced to
       the model as an error tool-result (the loop's dispatch try/except).
    2. **Budgets** — the archetype's max_iterations / max_tool_calls /
       max_input_tokens / timeout flow into the loop via MemoryProfile and are
       enforced there with safe, flagged stops.
    3. **Local tools** — opt-in tool names are added to the allowlist *only*
       for a profile that allows them (research). Other profiles can never call
       them, even if registered.
    4. **Audit** — when an AuditLog is given, a run_start/step/run_end trail is
       written (values never logged; see audit.py).
    """
    if provider is None:
        provider = get_provider()

    # Local tools are research-only (and only the ones actually registered).
    extra_allowed = local_tool_names if arch.allow_local_tools else frozenset()
    policy = ToolPolicy.from_archetype(arch, extra_allowed=extra_allowed)

    # The model is shown only allowlisted tools (soft); the wrapper is the hard gate.
    all_tools = await registry.list_tools()
    tools = filter_to_allowlist(all_tools, policy.allowlist)
    mem_profile = to_memory_profile(arch)

    # Audit wiring. Note: a refused call is already captured by the loop's
    # per-step audit hook (as a tool step with an error status), so we do NOT
    # also record a separate refusal here — that keeps the trail to exactly one
    # record per loop step.
    run_id: str | None = None
    hook = audit_hook
    if audit is not None:
        run_id = audit.start_run(arch.name, user_input)
        hook = audit.step_hook(run_id)

    enforcing = PolicyEnforcingRegistry(registry, policy)

    result = await run_grounded_agent(
        mem_profile, user_input, tools, provider, enforcing, audit_hook=hook
    )

    if audit is not None and run_id is not None:
        audit.end_run(run_id, result)

    return result


def extract_digest_id(result: GroundedAgentResult) -> str | None:
    """Find the digest id created by a mnesis_file_back call, if any.

    Scans the transcript for the file_back tool step and parses ``digest_id``
    from its result. Returns None when no digest was filed.
    """
    for step in result.transcript:
        if isinstance(step, ToolStep) and step.tool_name == "mnesis_file_back" and not step.is_error:
            try:
                data = json.loads(step.result)
            except (json.JSONDecodeError, ValueError, TypeError):
                continue
            if isinstance(data, dict):
                did = data.get("digest_id")
                if isinstance(did, str) and did:
                    return did
    return None


async def confirm_and_file(
    proposal,
    registry: ToolRegistry,
    *,
    quality_score: float | None = None,
) -> str:
    """File a propose-mode digest back to Mnesis after human confirmation.

    The assistant archetype proposes but never writes; the CLI calls this only
    once the human has confirmed. Returns the raw mnesis_file_back result.
    """
    args: dict = {"question": proposal.question, "answer": proposal.answer}
    if quality_score is not None:
        args["quality_score"] = quality_score
    return await registry.dispatch("mnesis_file_back", args)
