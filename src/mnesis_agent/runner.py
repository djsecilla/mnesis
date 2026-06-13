"""Shared run plumbing: build a registry, pick a provider, run an archetype.

This is the seam between the archetypes (A5) and the loop/memory core (A3/A4).
It builds the tool registry (an MCP client to Mnesis, plus any optional local
tool sources), selects the provider from config, filters tools to the
archetype's allowlist, and runs the grounded loop.
"""
from __future__ import annotations

import json
from typing import Callable

from . import config
from .loop import ToolStep
from .mcp_client import MCPToolSource, ToolSource
from .memory import GroundedAgentResult, run_grounded_agent
from .profiles import Archetype, filter_to_allowlist, to_memory_profile
from .provider import Provider, get_provider
from .registry import ToolRegistry


def build_registry(sources: list[ToolSource] | None = None) -> ToolRegistry:
    """Build the tool registry.

    With no ``sources``, connects to the configured Mnesis MCP endpoint. Pass
    explicit sources (e.g. a FakeToolSource, or Mnesis + local tools) to
    override — that is how tests and multi-source setups wire it.
    """
    registry = ToolRegistry()
    if sources is None:
        registry.add_source(MCPToolSource(config.MNESIS_MCP_URL, config.MNESIS_MCP_TOKEN))
    else:
        for src in sources:
            registry.add_source(src)
    return registry


async def run_archetype(
    arch: Archetype,
    user_input: str,
    registry: ToolRegistry,
    provider: Provider | None = None,
    *,
    audit_hook: Callable[[dict], None] | None = None,
) -> GroundedAgentResult:
    """Run one archetype against the registry.

    Lists the registry's tools, filters them to the archetype's allowlist
    (so the model only ever sees permitted tools), projects the archetype to a
    MemoryProfile, and runs the grounded loop.
    """
    if provider is None:
        provider = get_provider()

    all_tools = await registry.list_tools()
    tools = filter_to_allowlist(all_tools, arch.tool_allowlist)
    mem_profile = to_memory_profile(arch)

    return await run_grounded_agent(
        mem_profile, user_input, tools, provider, registry, audit_hook=audit_hook
    )


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
