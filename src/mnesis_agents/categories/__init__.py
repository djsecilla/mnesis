"""The three agent-category abstractions (ABCs) over the F4 base agent, plus
clearly-marked *smoke* example subclasses used only to prove the wiring runs.

Categories (trigger / write policy):
  - WritingAgent     : event           / ingest
  - ActionAgent      : event_or_schedule / propose
  - MaintenanceAgent : schedule        / propose

The Smoke* classes are SCAFFOLDING — trivial no-op/echo agents for tests, not
real agents. No production agents or source connectors live here.
"""
from __future__ import annotations

from typing import Any

from .action import ActionAgent
from .base import CategoryAgent
from .maintenance import MaintenanceAgent
from .writing import WritingAgent

__all__ = [
    "CategoryAgent",
    "WritingAgent",
    "ActionAgent",
    "MaintenanceAgent",
    "SmokeWritingAgent",
    "SmokeActionAgent",
    "SmokeMaintenanceAgent",
]


# ── Smoke example subclasses (SCAFFOLDING — not real agents) ────────────────


class SmokeWritingAgent(WritingAgent):
    """SCAFFOLDING: echoes an inbound artifact as the text to ingest."""

    input_shape = "a plain string (smoke)"

    def system_prompt(self) -> str:
        return "You are a smoke writing agent (scaffolding). Ingest the given source."

    def parse_artifact(self, event: Any) -> str:
        return str(event)

    def source_ref(self, event: Any) -> str:
        return "smoke-source"


class SmokeActionAgent(ActionAgent):
    """SCAFFOLDING: its action channel is a trivial echo tool."""

    def system_prompt(self) -> str:
        return "You are a smoke action agent (scaffolding). Read Mnesis, then echo."

    def action_tools(self):
        from langchain_core.tools import tool

        @tool
        def echo(text: str) -> str:
            """(smoke) Echo the given text back as the external action."""
            return f"echo: {text}"

        return [echo]


class SmokeMaintenanceAgent(MaintenanceAgent):
    """SCAFFOLDING: a no-op dream-cycle agent with a fixed cadence/scope."""

    def system_prompt(self) -> str:
        return "You are a smoke maintenance agent (scaffolding). Review and report."

    def cadence(self) -> str:
        return "manual"

    def scope(self) -> list[str]:
        return ["smoke"]
