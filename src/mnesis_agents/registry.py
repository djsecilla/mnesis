"""AgentRegistry — agents subscribe to the trigger(s) that should fire them.

Triggering is decoupled from agent logic: a subscription pairs a trigger
(event filter or schedule) with an async **handler** that does the work. The
runner only knows about subscriptions + handlers, never about agent internals.
Convenience helpers wrap an F4 ``Agent`` into a handler for the common cases.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .triggers.events import InboundEvent
from .triggers.schedule import Schedule

if TYPE_CHECKING:
    from .base import Agent

#: An event handler: receives the inbound event, returns anything (e.g. AgentResult).
EventHandler = Callable[[InboundEvent], Awaitable[Any]]
#: A schedule handler: receives nothing, returns anything.
ScheduleHandler = Callable[[], Awaitable[Any]]


@dataclass(frozen=True)
class EventSubscription:
    name: str
    handler: EventHandler
    source: str | None = None   # match filter (None = any)
    kind: str | None = None

    def matches(self, event: InboundEvent) -> bool:
        return (self.source in (None, event.source)) and (self.kind in (None, event.kind))


@dataclass(frozen=True)
class ScheduleSubscription:
    name: str
    handler: ScheduleHandler
    schedule: Schedule


@dataclass
class AgentRegistry:
    """Holds the trigger subscriptions the runner dispatches to."""

    event_subs: list[EventSubscription] = field(default_factory=list)
    schedule_subs: list[ScheduleSubscription] = field(default_factory=list)

    # -- low-level (generic handlers) ------------------------------------------

    def on_event(
        self, name: str, handler: EventHandler, *, source: str | None = None, kind: str | None = None
    ) -> EventSubscription:
        sub = EventSubscription(name=name, handler=handler, source=source, kind=kind)
        self.event_subs.append(sub)
        return sub

    def on_schedule(self, name: str, handler: ScheduleHandler, schedule: Schedule) -> ScheduleSubscription:
        sub = ScheduleSubscription(name=name, handler=handler, schedule=schedule)
        self.schedule_subs.append(sub)
        return sub

    # -- convenience (wrap an F4 Agent) ----------------------------------------

    def register_agent_on_event(
        self,
        agent: "Agent",
        name: str,
        *,
        source: str | None = None,
        kind: str | None = None,
        to_input: Callable[[InboundEvent], str] | None = None,
    ) -> EventSubscription:
        """Fire ``agent`` on matching events, deriving its input from the event."""
        derive = to_input or (lambda e: str(e.payload))

        async def handler(event: InboundEvent) -> Any:
            return await agent.arun(derive(event))

        return self.on_event(name, handler, source=source, kind=kind)

    def register_agent_on_schedule(
        self,
        agent: "Agent",
        name: str,
        schedule: Schedule,
        *,
        prompt: str = "Run your scheduled task.",
    ) -> ScheduleSubscription:
        """Fire ``agent`` on a schedule with a fixed prompt."""

        async def handler() -> Any:
            return await agent.arun(prompt)

        return self.on_schedule(name, handler, schedule)

    # -- introspection ----------------------------------------------------------

    @property
    def is_empty(self) -> bool:
        return not self.event_subs and not self.schedule_subs
