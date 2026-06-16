"""Guardrails, persistence, and human-in-the-loop for the base agent.

- **GovernanceMiddleware** enforces, *before any tool side effect* (fail-closed):
  the tool allowlist, the write policy (propose vs apply), and per-run budgets
  (tool calls / tokens / wall-clock). Refusals are returned to the model as error
  ToolMessages so it can adapt; budget/wall-clock exhaustion jumps the run to end
  with a flagged ``stop_reason``.
- **make_checkpointer** builds a LangGraph checkpointer (SQLite default, memory
  optional; Postgres via the ``agents-postgres`` extra) so threads are durable
  and resumable.
- **build_approval_middleware** wires LangChain's HumanInTheLoopMiddleware so
  configured (risky) tools — e.g. a supersession or an external send — pause the
  run for approval and resume on a supplied decision.

Per-write SAFETY (secret/PII redaction, contradiction/supersession review) is and
remains enforced by **Mnesis itself**, server-side, on every ingest/file_back.
Governance here decides *whether* an agent may call a write, never *how* the write
is made safe.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from langchain.agents.middleware import AgentMiddleware, HumanInTheLoopMiddleware
from langchain.agents.middleware.types import hook_config
from langchain_core.messages import ToolMessage

from . import config


@dataclass
class GovernanceState:
    """Per-run governance outcome (read by the Agent for the result + audit)."""

    tool_calls: int = 0
    tokens: int = 0
    started: float = 0.0
    stop_reason: str | None = None
    refusals: list[dict[str, Any]] = field(default_factory=list)
    writes: list[dict[str, Any]] = field(default_factory=list)
    proposals: list[dict[str, Any]] = field(default_factory=list)


#: Write policies under which a Mnesis write tool actually executes. Under any
#: other policy (off / propose) the write is proposed, not applied.
_WRITE_EXECUTE_POLICIES: frozenset[str] = frozenset({"apply", "ingest", "approved"})


def _bare(name: str) -> str:
    return name.split("__", 1)[-1]  # tolerate registry namespacing


class GovernanceMiddleware(AgentMiddleware):
    """Allowlist + write-policy + budget enforcement, fail-closed.

    Note: per-run counters live on the instance and are reset by ``begin_run()``
    (the Agent calls it before a fresh run, not on resume). This is safe for
    sequential runs; concurrent runs on one compiled agent would need per-thread
    state (a documented future hardening).
    """

    def __init__(
        self,
        *,
        allowlist: frozenset[str] | None = None,
        write_tools: frozenset[str] = frozenset(),
        write_policy: str = "off",
        max_tool_calls: int | None = None,
        max_tokens: int | None = None,
        wallclock_seconds: float | None = None,
    ) -> None:
        super().__init__()
        self.allowlist = allowlist
        self.write_tools = write_tools
        self.write_policy = write_policy
        self.max_tool_calls = max_tool_calls
        self.max_tokens = max_tokens
        self.wallclock = wallclock_seconds
        self.state = GovernanceState()

    def begin_run(self) -> None:
        self.state = GovernanceState(started=time.monotonic())

    # -- tool gate (before any side effect) ------------------------------------

    def wrap_tool_call(self, request, handler):
        tc = request.tool_call
        name = tc.get("name", "")
        tcid = tc.get("id", "")
        bare = _bare(name)

        def refuse(reason: str, message: str) -> ToolMessage:
            self.state.refusals.append({"tool": name, "reason": reason})
            return ToolMessage(content=f"Refused: {message}", tool_call_id=tcid, status="error")

        # 1) Allowlist — fail closed.
        if self.allowlist is not None and bare not in self.allowlist and name not in self.allowlist:
            return refuse("allowlist", f"tool {name!r} is not in this agent's allowlist.")

        # 2) Tool-call budget — refuse before executing, flag the run.
        if self.max_tool_calls is not None and self.state.tool_calls >= self.max_tool_calls:
            self.state.stop_reason = self.state.stop_reason or "tool_budget"
            return refuse("tool_budget", f"tool-call budget ({self.max_tool_calls}) exhausted.")

        # 3) Write policy — execute only under apply/ingest/approved; else propose.
        is_write = bare in self.write_tools or name in self.write_tools
        if is_write and self.write_policy not in _WRITE_EXECUTE_POLICIES:
            self.state.proposals.append({"tool": name})
            return refuse(
                "write_policy",
                f"write tool {name!r} proposed, not applied (write_policy={self.write_policy!r}).",
            )

        # Execute — count it; record applied writes (arg KEYS only, never values).
        self.state.tool_calls += 1
        if is_write:
            self.state.writes.append({"tool": name, "args_keys": sorted((tc.get("args") or {}))})
        return handler(request)

    # -- run-level budgets (jump to end when exceeded) -------------------------

    @hook_config(can_jump_to=["end"])
    def before_model(self, state, runtime):  # noqa: ANN001, ARG002
        if self.state.stop_reason:  # budget already tripped in a tool gate
            return {"jump_to": "end"}
        if self.wallclock is not None and self.state.started:
            if time.monotonic() - self.state.started > self.wallclock:
                self.state.stop_reason = "deadline"
                return {"jump_to": "end"}
        if self.max_tokens and self.state.tokens >= self.max_tokens:
            self.state.stop_reason = "token_budget"
            return {"jump_to": "end"}
        return None

    def after_model(self, state, runtime):  # noqa: ANN001, ARG002
        # Best-effort token accounting from the latest AI message.
        msgs = state.get("messages", []) if isinstance(state, dict) else []
        if msgs:
            usage = getattr(msgs[-1], "usage_metadata", None) or {}
            self.state.tokens += int(usage.get("total_tokens", 0) or 0)
        return None


# ── Checkpointer ────────────────────────────────────────────────────────────


def make_checkpointer(backend: str | None = None, *, db_path=None):
    """Build a LangGraph checkpointer. ``sqlite`` (default, durable) or ``memory``.

    SQLite uses a process-lifetime connection to the configured DB path; the
    Postgres backend is available via the ``agents-postgres`` extra (wire it the
    same way). Returns a checkpointer instance ready to pass to build_agent.
    """
    backend = (backend or config.MNESIS_AGENTS_CHECKPOINT_BACKEND).lower()
    if backend == "memory":
        from langgraph.checkpoint.memory import InMemorySaver

        return InMemorySaver()
    if backend == "sqlite":
        import sqlite3

        from langgraph.checkpoint.sqlite import SqliteSaver

        path = str(db_path or config.MNESIS_AGENTS_CHECKPOINT_DB)
        conn = sqlite3.connect(path, check_same_thread=False)
        return SqliteSaver(conn)
    raise ValueError(f"unknown checkpoint backend {backend!r} (use 'sqlite' or 'memory')")


# ── Human-in-the-loop approval ──────────────────────────────────────────────


def build_approval_middleware(approval_tools: frozenset[str]):
    """A HumanInTheLoopMiddleware that pauses before each approval-required tool.

    Returns None when no tools need approval (so no interrupt machinery is added).
    """
    if not approval_tools:
        return None
    return HumanInTheLoopMiddleware(interrupt_on={name: True for name in approval_tools})
