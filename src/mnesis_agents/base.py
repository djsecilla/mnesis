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
    write_tools: frozenset[str] = DEFAULT_WRITE_TOOLS
    write_policy: str = "off"
    recursion_limit: int = 25
    checkpointer: Any = None  # F6 configures this (e.g. a LangGraph checkpointer)


@dataclass
class AgentResult:
    """Structured outcome of an agent run."""

    output: str                 # final assistant text
    messages: list[Any]         # full LangGraph message list
    steps: int                  # model turns taken
    tools_used: list[str]       # distinct tool names called (in call order)
    skills_used: list[str]      # skills activated via use_skill
    writes: list[dict[str, Any]]  # write-tool calls performed ({tool, args_keys})


def _is_write(name: str, write_tools: frozenset[str]) -> bool:
    bare = name.split("__", 1)[-1]  # tolerate registry namespacing
    return name in write_tools or bare in write_tools


def build_agent(profile: AgentProfile, *, model=None) -> "Agent":
    """Compile a LangGraph agent from a profile and return an :class:`Agent`.

    Skills are exposed two ways (model-agnostic, per F3): their cards are appended
    to the system prompt (discovery), and a ``use_skill`` tool is added so the
    model can activate one on demand. ``model`` defaults to the configured one
    (F1) — tests inject a scripted stub.
    """
    from langchain.agents import create_agent

    model = model if model is not None else get_chat_model()
    tools = list(profile.tools)
    system = profile.system_prompt

    if profile.skills is not None:
        from .skills.loader import make_use_skill_tool

        system = f"{system}\n\n{profile.skills.cards_prompt()}"
        tools.append(make_use_skill_tool(profile.skills))

    graph = create_agent(
        model,
        tools,
        system_prompt=system,
        checkpointer=profile.checkpointer,
        name=profile.name,
    )
    return Agent(graph=graph, profile=profile)


class Agent:
    """A compiled LangGraph agent plus a structured run interface."""

    def __init__(self, graph, profile: AgentProfile) -> None:
        self.graph = graph
        self.profile = profile

    # -- run --------------------------------------------------------------------

    def _input(self, text: str) -> dict[str, Any]:
        return {"messages": [{"role": "user", "content": text}]}

    def _config(self, config: dict | None) -> dict:
        cfg = {"recursion_limit": self.profile.recursion_limit}
        if config:
            cfg.update(config)
        return cfg

    def run(self, input: str, *, config: dict | None = None) -> AgentResult:
        state = self.graph.invoke(self._input(input), config=self._config(config))
        return self._result(state)

    async def arun(self, input: str, *, config: dict | None = None) -> AgentResult:
        state = await self.graph.ainvoke(self._input(input), config=self._config(config))
        return self._result(state)

    def astream(self, input: str, *, config: dict | None = None):
        """Passthrough to the compiled graph's async stream (raw LangGraph events)."""
        return self.graph.astream(self._input(input), config=self._config(config))

    # -- structured result ------------------------------------------------------

    def _result(self, state: dict[str, Any]) -> AgentResult:
        messages = state.get("messages", [])
        tools_used: list[str] = []
        skills_used: list[str] = []
        writes: list[dict[str, Any]] = []
        steps = 0
        final = ""

        for m in messages:
            tool_calls = getattr(m, "tool_calls", None) or []
            if getattr(m, "type", None) == "ai" or tool_calls:
                steps += 1
            for tc in tool_calls:
                name = tc.get("name", "")
                args = tc.get("args", {}) or {}
                tools_used.append(name)
                if name.split("__", 1)[-1] == "use_skill":
                    skill = args.get("name")
                    if skill:
                        skills_used.append(str(skill))
                if _is_write(name, self.profile.write_tools):
                    writes.append({"tool": name, "args_keys": sorted(args)})
            # last AI text content is the final output
            if getattr(m, "type", None) == "ai" and isinstance(getattr(m, "content", None), str) and m.content:
                final = m.content

        # de-dupe tools_used preserving order
        seen: set[str] = set()
        tools_unique = [t for t in tools_used if not (t in seen or seen.add(t))]
        return AgentResult(
            output=final,
            messages=messages,
            steps=steps,
            tools_used=tools_unique,
            skills_used=list(dict.fromkeys(skills_used)),
            writes=writes,
        )
