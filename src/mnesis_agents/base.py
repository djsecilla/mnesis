"""The reusable base agent — model + tools + skills + memory + guardrails on LangGraph.

``build_agent(profile)`` wires the F1 chat model, F2 Mnesis (and local) tools, and
the F3 skills registry into a compiled LangGraph agent via LangChain 1.x
``create_agent``, and returns an :class:`Agent` wrapper exposing ``run`` / ``arun``
/ ``astream`` and a structured :class:`AgentResult` (final output, steps, tools &
skills used, writes performed).

The base is provider- and tool-source-agnostic: it consumes already-resolved
``BaseChatModel`` + ``BaseTool``s + a ``SkillRegistry``. Agent categories (F4
``categories/``) add only their trigger and policy shape on top.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .models import get_chat_model

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

    from .skills.loader import SkillRegistry

#: Tool names treated as Mnesis writes (bare or ``<ns>__`` namespaced).
DEFAULT_WRITE_TOOLS: frozenset[str] = frozenset({"mnesis_ingest", "mnesis_file_back"})


@dataclass
class AgentProfile:
    """Everything the base needs to assemble one agent.

    ``write_policy`` is a declared contract (categories set it: ingest / propose /
    approved / off); Mnesis enforces *per-write* safety server-side regardless.
    """

    name: str
    system_prompt: str
    tools: list["BaseTool"] = field(default_factory=list)
    skills: "SkillRegistry | None" = None
    # Governance (F6).
    write_tools: frozenset[str] = DEFAULT_WRITE_TOOLS
    write_policy: str = "off"            # off | propose | apply | ingest | approved
    tool_allowlist: frozenset[str] | None = None  # None = allow all permitted tools
    approval_tools: frozenset[str] = frozenset()  # tools that pause for approval
    max_tool_calls: int | None = None
    max_tokens: int | None = None
    wallclock_seconds: float | None = None
    recursion_limit: int = 25
    checkpointer: Any = None  # LangGraph checkpointer (durable threads / interrupts)


@dataclass
class AgentResult:
    """Structured outcome of an agent run."""

    output: str                 # final assistant text
    messages: list[Any]         # full LangGraph message list
    steps: int                  # model turns taken
    tools_used: list[str]       # distinct tool names called (in call order)
    skills_used: list[str]      # skills activated via use_skill
    writes: list[dict[str, Any]] = field(default_factory=list)     # APPLIED writes ({tool, args_keys})
    refusals: list[dict[str, Any]] = field(default_factory=list)   # governance refusals ({tool, reason})
    stop_reason: str = "end"    # end | tool_budget | token_budget | deadline | interrupt
    usage: dict[str, Any] = field(default_factory=dict)
    interrupted: bool = False
    interrupt: Any = None       # the pending approval request when interrupted
    thread_id: str | None = None


def build_agent(profile: AgentProfile, *, model=None) -> "Agent":
    """Compile a LangGraph agent from a profile and return an :class:`Agent`.

    Skills are exposed two ways (model-agnostic, per F3): their cards are appended
    to the system prompt (discovery), and a ``use_skill`` tool is added so the
    model can activate one on demand. ``model`` defaults to the configured one
    (F1) — tests inject a scripted stub.
    """
    from langchain.agents import create_agent

    from .governance import GovernanceMiddleware, build_approval_middleware, make_checkpointer

    model = model if model is not None else get_chat_model()
    tools = list(profile.tools)
    system = profile.system_prompt

    allowlist = profile.tool_allowlist
    if profile.skills is not None:
        from .skills.loader import make_use_skill_tool

        system = f"{system}\n\n{profile.skills.cards_prompt()}"
        tools.append(make_use_skill_tool(profile.skills))
        if allowlist is not None:
            allowlist = allowlist | {"use_skill"}  # never refuse the skills tool

    governance = GovernanceMiddleware(
        allowlist=allowlist,
        write_tools=profile.write_tools,
        write_policy=profile.write_policy,
        max_tool_calls=profile.max_tool_calls,
        max_tokens=profile.max_tokens,
        wallclock_seconds=profile.wallclock_seconds,
    )
    middleware: list = [governance]
    approval = build_approval_middleware(profile.approval_tools)
    if approval is not None:
        middleware.append(approval)

    # Interrupts (approval) need a checkpointer; default to in-memory if none set.
    checkpointer = profile.checkpointer
    if checkpointer is None and profile.approval_tools:
        checkpointer = make_checkpointer("memory")

    graph = create_agent(
        model, tools,
        system_prompt=system,
        middleware=middleware,
        checkpointer=checkpointer,
        name=profile.name,
    )
    return Agent(graph=graph, profile=profile, governance=governance, checkpointed=checkpointer is not None)


class Agent:
    """A compiled LangGraph agent plus a structured, governed run interface."""

    def __init__(self, graph, profile: AgentProfile, *, governance=None, checkpointed: bool = False) -> None:
        self.graph = graph
        self.profile = profile
        self.governance = governance
        self.checkpointed = checkpointed

    # -- config helpers ---------------------------------------------------------

    def _input(self, text: str) -> dict[str, Any]:
        return {"messages": [{"role": "user", "content": text}]}

    def _config(self, thread_id: str | None, config: dict | None) -> tuple[dict, str | None]:
        import uuid

        cfg: dict[str, Any] = {"recursion_limit": self.profile.recursion_limit}
        tid = thread_id
        if self.checkpointed:
            tid = tid or uuid.uuid4().hex
            cfg["configurable"] = {"thread_id": tid}
        if config:
            cfg.update(config)
        return cfg, tid

    # -- run --------------------------------------------------------------------

    def run(self, input: str, *, thread_id: str | None = None, config: dict | None = None) -> AgentResult:
        if self.governance is not None:
            self.governance.begin_run()
        cfg, tid = self._config(thread_id, config)
        state = self.graph.invoke(self._input(input), config=cfg)
        return self._result(state, tid)

    async def arun(self, input: str, *, thread_id: str | None = None, config: dict | None = None) -> AgentResult:
        if self.governance is not None:
            self.governance.begin_run()
        cfg, tid = self._config(thread_id, config)
        state = await self.graph.ainvoke(self._input(input), config=cfg)
        return self._result(state, tid)

    def astream(self, input: str, *, thread_id: str | None = None, config: dict | None = None):
        if self.governance is not None:
            self.governance.begin_run()
        cfg, _ = self._config(thread_id, config)
        return self.graph.astream(self._input(input), config=cfg)

    # -- resume (after an approval interrupt; does NOT reset governance) --------

    def resume(self, decision: Any, *, thread_id: str, config: dict | None = None) -> AgentResult:
        from langgraph.types import Command

        cfg, tid = self._config(thread_id, config)
        state = self.graph.invoke(Command(resume=decision), config=cfg)
        return self._result(state, tid)

    def approve(self, *, thread_id: str) -> AgentResult:
        """Approve a pending interrupt and resume the run."""
        return self.resume({"decisions": [{"type": "approve"}]}, thread_id=thread_id)

    def reject(self, message: str = "Rejected.", *, thread_id: str) -> AgentResult:
        """Reject a pending interrupt (the tool is not executed) and resume."""
        return self.resume({"decisions": [{"type": "reject", "message": message}]}, thread_id=thread_id)

    # -- structured result ------------------------------------------------------

    def _result(self, state: dict[str, Any], thread_id: str | None) -> AgentResult:
        messages = state.get("messages", []) if isinstance(state, dict) else []
        interrupt = state.get("__interrupt__") if isinstance(state, dict) else None

        tools_used: list[str] = []
        skills_used: list[str] = []
        steps = 0
        final = ""
        for m in messages:
            tool_calls = getattr(m, "tool_calls", None) or []
            if getattr(m, "type", None) == "ai":
                steps += 1
            for tc in tool_calls:
                name = tc.get("name", "")
                tools_used.append(name)
                if name.split("__", 1)[-1] == "use_skill":
                    skill = (tc.get("args") or {}).get("name")
                    if skill:
                        skills_used.append(str(skill))
            if getattr(m, "type", None) == "ai" and isinstance(getattr(m, "content", None), str) and m.content:
                final = m.content

        gov = self.governance
        seen: set[str] = set()
        return AgentResult(
            output=final,
            messages=messages,
            steps=steps,
            tools_used=[t for t in tools_used if not (t in seen or seen.add(t))],
            skills_used=list(dict.fromkeys(skills_used)),
            writes=list(gov.state.writes) if gov else [],
            refusals=list(gov.state.refusals) if gov else [],
            stop_reason="interrupt" if interrupt else ((gov.state.stop_reason if gov else None) or "end"),
            usage={"total_tokens": gov.state.tokens} if gov else {},
            interrupted=bool(interrupt),
            interrupt=interrupt,
            thread_id=thread_id,
        )
