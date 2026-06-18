"""Governed, deterministic tool dispatch — shared by the mechanical agents.

The dream cycle and the writing agent drive Mnesis MCP tools **directly**, outside
an LLM loop. They still run under F6: this reuses
:meth:`GovernanceMiddleware._gate` so the allowlist, write-policy, and tool-call
budget are enforced *identically* to an agent run; wall-clock is checked here (the
gate's wall-clock hook only fires inside the model loop).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .governance import GovernanceMiddleware

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool


class BudgetStop(Exception):
    """A governed call was refused because a budget/wall-clock limit tripped."""


class ToolRefused(Exception):
    """A governed call was refused for a non-budget reason (allowlist/policy/missing)."""


@dataclass
class Call:
    name: str
    ok: bool
    output: str | None = None
    refusal: str | None = None


class GovernedTools:
    """Dispatch MCP tools through F6 governance, deterministically.

    ``id_prefix`` only labels the synthetic tool-call ids (telemetry); it has no
    behavioural effect.
    """

    def __init__(
        self, tools: list["BaseTool"], governance: GovernanceMiddleware, *, id_prefix: str = "call"
    ) -> None:
        self._by_name: dict[str, BaseTool] = {}
        for t in tools:
            self._by_name[t.name] = t
            self._by_name.setdefault(t.name.split("__", 1)[-1], t)  # tolerate namespacing
        self._gov = governance
        self._id_prefix = id_prefix
        self.executed: list[dict[str, Any]] = []

    def stopped(self) -> bool:
        return bool(self._gov.state.stop_reason)

    def stop_reason(self) -> str | None:
        return self._gov.state.stop_reason

    def call(self, name: str, args: dict[str, Any]) -> Call:
        # Wall-clock first (the gate doesn't check it outside the model loop).
        g = self._gov
        if g.wallclock and g.state.started and (time.monotonic() - g.state.started) > g.wallclock:
            g.state.stop_reason = g.state.stop_reason or "deadline"
            return Call(name, ok=False, refusal="wall-clock budget exceeded")

        tc = {"name": name, "args": args, "id": f"{self._id_prefix}-{len(self.executed)}"}
        refusal = g._gate(tc)
        if refusal is not None:
            return Call(name, ok=False, refusal=str(getattr(refusal, "content", refusal)))

        tool = self._by_name.get(name)
        if tool is None:
            return Call(name, ok=False, refusal=f"tool {name!r} not available")
        output = tool.invoke(args)
        self.executed.append({"tool": name, "args_keys": sorted(args)})
        return Call(name, ok=True, output=output if isinstance(output, str) else str(output))

    def require(self, name: str, args: dict[str, Any]) -> Call:
        """Call a tool, raising on refusal so the caller can classify it."""
        c = self.call(name, args)
        if not c.ok:
            if self.stopped():
                raise BudgetStop(self.stop_reason() or "budget")
            raise ToolRefused(c.refusal or "refused")
        return c
