"""Hard policy enforcement: allowlist + write-policy gate before every dispatch.

The archetypes (A5) filter the tool list *shown* to the model to the profile's
allowlist — a soft constraint (the model can't call what it can't see). This
module is the *hard* gate: even if a tool call for a forbidden tool reaches
dispatch (a misbehaving model, a crafted call, a future code path), it is
refused deterministically **before any side effect**.

``PolicyEnforcingRegistry`` wraps a ``ToolRegistry`` and runs ``ToolPolicy.check``
before delegating. A violation raises ``PolicyViolation``; the loop (A3) catches
any dispatch exception and feeds it back to the model as an error tool-result,
so a refusal is *surfaced to the model* and the run can recover or conclude.

Per-write safety — secret/PII redaction and contradiction/supersession review —
is enforced by **Mnesis itself**, server-side, on every ingest/file_back. The
agent only *calls* the tool; it cannot reach Mnesis's internals and therefore
**cannot bypass that governance**. This layer governs *whether* the agent may
call a write tool at all (by profile + write policy), not *how* the write is
made safe.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .memory import DEFAULT_WRITE_TOOLS, WritePolicy
from .mcp_client import ToolSpec
from .registry import ToolRegistry


class PolicyViolation(RuntimeError):
    """A tool call was refused by policy (out-of-allowlist or write-policy)."""


@dataclass(frozen=True)
class ToolPolicy:
    """The per-run rules: what may be called, and which writes are permitted.

    ``allowlist``        every tool name the run may call.
    ``write_policy``     off | propose | apply (writes only under ``apply``).
    ``write_tools``      the universe of tool names that count as writes.
    ``write_allowlist``  the subset of writes permitted under ``apply``.
    """

    allowlist: frozenset[str]
    write_policy: WritePolicy
    write_tools: frozenset[str] = DEFAULT_WRITE_TOOLS
    write_allowlist: frozenset[str] = DEFAULT_WRITE_TOOLS

    @classmethod
    def from_archetype(cls, arch, *, extra_allowed: frozenset[str] = frozenset()) -> "ToolPolicy":
        """Build a ToolPolicy from an Archetype.

        ``extra_allowed`` adds tool names to the allowlist (used for opt-in local
        tools, which the runner permits only for profiles that allow them).
        """
        write_allow = (
            arch.write_allowlist if arch.write_allowlist is not None else DEFAULT_WRITE_TOOLS
        )
        return cls(
            allowlist=arch.tool_allowlist | extra_allowed,
            write_policy=arch.write_policy,
            write_tools=DEFAULT_WRITE_TOOLS,
            write_allowlist=write_allow,
        )

    def check(self, name: str) -> None:
        """Raise PolicyViolation if ``name`` may not be dispatched. No side effects."""
        if name not in self.allowlist:
            raise PolicyViolation(
                f"Tool {name!r} is not in this profile's allowlist "
                f"({sorted(self.allowlist)}); refused."
            )
        if name in self.write_tools:
            if self.write_policy != "apply":
                raise PolicyViolation(
                    f"Write tool {name!r} refused: write policy is "
                    f"{self.write_policy!r} (writes require 'apply')."
                )
            if name not in self.write_allowlist:
                raise PolicyViolation(
                    f"Write tool {name!r} refused: not in this profile's write "
                    f"allowlist ({sorted(self.write_allowlist)})."
                )


class PolicyEnforcingRegistry:
    """A drop-in registry wrapper that enforces ``ToolPolicy`` before dispatch.

    Duck-compatible with ToolRegistry where the loop/memory layer uses it
    (``dispatch`` and ``list_tools``). On a refusal it raises PolicyViolation
    *before* delegating, so no side effect occurs. The optional ``on_refusal``
    callback receives the tool name for auditing (no args, no values).
    """

    def __init__(
        self,
        inner: ToolRegistry,
        policy: ToolPolicy,
        *,
        on_refusal=None,
    ) -> None:
        self._inner = inner
        self._policy = policy
        self._on_refusal = on_refusal

    @property
    def policy(self) -> ToolPolicy:
        return self._policy

    async def list_tools(self) -> list[ToolSpec]:
        return await self._inner.list_tools()

    async def dispatch(self, name: str, args: dict) -> str:
        try:
            self._policy.check(name)
        except PolicyViolation as exc:
            if self._on_refusal is not None:
                self._on_refusal(name)
            raise  # surfaced to the model by the loop's dispatch try/except
        return await self._inner.dispatch(name, args)
