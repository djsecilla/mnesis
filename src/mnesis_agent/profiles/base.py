"""Archetype definition and the mapping to the memory/loop layer.

An **archetype** is one profile over the same agent core: a system prompt, a
tool allowlist, a write policy, budgets, and an entry mode.  Three archetypes
(assistant, research, ingest-daemon) live in sibling modules; this module holds
the shared dataclass and the helpers that turn an archetype into the
``MemoryProfile`` the loop consumes and that filter tools to the allowlist.

The allowlist is the soft contract here: tools outside it are never shown to
the model, so it cannot call them.  Hard refusal at dispatch time is A6.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..mcp_client import ToolSpec
from ..memory import MemoryProfile, WritePolicy

#: How an archetype is driven.
EntryMode = Literal["interactive", "batch", "daemon"]


@dataclass(frozen=True)
class Archetype:
    """One agent profile: prompt + allowlist + write policy + budgets + entry mode.

    ``tool_allowlist`` is the complete set of tool names the agent may use.
    ``write_allowlist`` (a subset) is the set that count as writes under an
    ``apply`` policy; ``None`` means "the memory layer's default write tools".
    """

    name: str
    system_prompt: str
    tool_allowlist: frozenset[str]
    write_policy: WritePolicy
    write_allowlist: frozenset[str] | None = None

    # Budgets (forwarded to the loop via MemoryProfile).
    max_iterations: int = 10
    max_tool_calls: int = 30
    max_input_tokens: int = 50_000
    timeout_seconds: float | None = None
    context_limit: int = 5

    entry_mode: EntryMode = "interactive"

    #: Whether this profile may use opt-in local tools (A6). Only research does.
    allow_local_tools: bool = False


def to_memory_profile(arch: Archetype) -> MemoryProfile:
    """Project an Archetype onto the MemoryProfile the grounded loop consumes."""
    return MemoryProfile(
        base_system=arch.system_prompt,
        max_iterations=arch.max_iterations,
        max_tool_calls=arch.max_tool_calls,
        max_input_tokens=arch.max_input_tokens,
        timeout_seconds=arch.timeout_seconds,
        write_policy=arch.write_policy,
        write_allowlist=arch.write_allowlist,
        context_limit=arch.context_limit,
    )


def filter_to_allowlist(
    tools: list[ToolSpec], allowlist: frozenset[str]
) -> list[ToolSpec]:
    """Return only the tools whose name is in the allowlist (order preserved)."""
    return [t for t in tools if t.name in allowlist]
