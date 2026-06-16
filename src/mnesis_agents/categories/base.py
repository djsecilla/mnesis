"""Shared base for the three agent categories.

A category adds only a **trigger** and a **write-policy** shape on top of the F4
base agent; it does NOT change how the base wires model/tools/skills. Each
category is an ABC declaring the abstract members a concrete agent must supply;
instantiating an incomplete subclass fails clearly (standard ABC behaviour).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

from ..base import Agent, AgentProfile, build_agent

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

    from ..skills.loader import SkillRegistry


class CategoryAgent(ABC):
    """Base for a category of agents over the F4 base agent.

    Subclasses set the class-level contract (``category``/``trigger``/
    ``write_policy``) and implement ``system_prompt``. The category-specific ABCs
    add further abstract members (e.g. how to parse a source, the action channel,
    the maintenance cadence).
    """

    #: Declared contract — set by each category ABC.
    category: ClassVar[str] = "agent"
    trigger: ClassVar[str] = "event"          # "event" | "schedule" | "event_or_schedule"
    write_policy: ClassVar[str] = "off"       # off | propose | approved | ingest

    def __init__(
        self,
        *,
        tools: "list[BaseTool] | None" = None,
        skills: "SkillRegistry | None" = None,
        model=None,
    ) -> None:
        # Resolved Mnesis/local tools (from the F2 registry) and the F3 skills —
        # the base stays tool-source-agnostic, so categories just pass them down.
        self._extra_tools = list(tools or [])
        self._skills = skills
        self._model = model

    @abstractmethod
    def system_prompt(self) -> str:
        """The agent's system prompt (category- and task-specific)."""

    def write_tools(self) -> frozenset[str]:
        """Mnesis tool names this agent is expected to use as writes."""
        return frozenset()

    def tools(self) -> "list[BaseTool]":
        """The full tool set for this agent (base = the injected tools)."""
        return list(self._extra_tools)

    def build_profile(self) -> AgentProfile:
        return AgentProfile(
            name=self.category,
            system_prompt=self.system_prompt(),
            tools=self.tools(),
            skills=self._skills,
            write_tools=self.write_tools(),
            write_policy=self.write_policy,
        )

    def build(self) -> Agent:
        """Compile this agent (provider/model resolved by the base or injected)."""
        return build_agent(self.build_profile(), model=self._model)
