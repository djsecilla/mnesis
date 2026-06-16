"""ActionAgent — the read-reason-act category.

Trigger: an event OR a schedule. The agent reads Mnesis (grounded retrieval),
reasons, and performs an external action through an **action tool / channel**
that the concrete subclass supplies (left abstract here — e.g. send a message,
open a PR, call an API). Write policy is ``propose`` by default: external effects
should be proposed or human-approved, not fired blindly. Any write-back to Mnesis
(e.g. crystallizing a digest) still goes through Mnesis governance.
"""
from __future__ import annotations

from abc import abstractmethod
from typing import ClassVar, TYPE_CHECKING

from .base import CategoryAgent

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool


class ActionAgent(CategoryAgent):
    """Abstract base for agents that act on the world from Mnesis knowledge.

    Concrete subclasses must implement ``action_tools`` — the external action
    channel (kept abstract: the base never assumes a specific effect surface).
    Those tools are added to the agent's tool set alongside the injected Mnesis
    read tools.
    """

    category: ClassVar[str] = "action"
    trigger: ClassVar[str] = "event_or_schedule"
    write_policy: ClassVar[str] = "propose"   # external effects proposed/approved

    @abstractmethod
    def action_tools(self) -> "list[BaseTool]":
        """The external action channel as LangChain tools (the abstract interface)."""

    def tools(self) -> "list[BaseTool]":
        return [*self._extra_tools, *self.action_tools()]
