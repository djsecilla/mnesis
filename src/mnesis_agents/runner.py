"""Runner — wires triggers → registry → agent execution.

Consumes events from the given event triggers and fires the registry's schedule
subscriptions, dispatching each to the matching agents. Resilient (one failing
run is caught and recorded, never stops the runner) and observable (every
dispatch yields a :class:`RunRecord`). Governance (F6) is applied per run via the
agent handlers / profiles; the runner stays policy-agnostic.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from .config import now_iso as _now
from .registry import AgentRegistry
from .triggers.events import EventTrigger, InboundEvent
from .triggers.schedule import AsyncIntervalScheduler

log = logging.getLogger("mnesis_agents.runner")


@dataclass
class RunRecord:
    """An observable record of one dispatch."""

    id: str
    subscription: str
    trigger: str            # "event:<source>/<kind>" or "schedule:<name>"
    status: str             # "ok" | "error"
    started_at: str
    ended_at: str
    error: str | None = None


@dataclass
class Runner:
    """Dispatches triggers to subscribed agents, resiliently and observably."""

    registry: AgentRegistry
    event_triggers: list[EventTrigger] = field(default_factory=list)
    #: Called with each RunRecord as it completes (in addition to being stored).
    observer: Callable[[RunRecord], None] | None = None
    max_records: int = 1000

    records: list[RunRecord] = field(default_factory=list)
    _consume_tasks: list[asyncio.Task] = field(default_factory=list, init=False)
    _scheduler: AsyncIntervalScheduler = field(default_factory=AsyncIntervalScheduler, init=False)
    _running: bool = field(default=False, init=False)
    _shutdown: asyncio.Event = field(default_factory=asyncio.Event, init=False)

    # -- lifecycle --------------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._shutdown.clear()
        # Consume each event trigger.
        for trigger in self.event_triggers:
            self._consume_tasks.append(asyncio.create_task(self._consume(trigger)))
        # Register schedule jobs (interval only in the bundled scheduler).
        for sub in self.registry.schedule_subs:
            self._scheduler.add_job(
                sub.name, sub.schedule,
                self._schedule_job(sub.name, sub.handler),
            )
        await self._scheduler.start()
        log.info(
            "runner started: %d event trigger(s), %d schedule job(s), %d event sub(s)",
            len(self.event_triggers), len(self.registry.schedule_subs), len(self.registry.event_subs),
        )

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        for t in self._consume_tasks:
            t.cancel()
        if self._consume_tasks:
            await asyncio.gather(*self._consume_tasks, return_exceptions=True)
        self._consume_tasks.clear()
        # Halt any stateful triggers (source connectors) cleanly — their detection
        # loops (poll/watch) run independently of the consume task.
        for trigger in self.event_triggers:
            stop = getattr(trigger, "stop", None)
            if callable(stop):
                try:
                    await stop()
                except Exception:  # noqa: BLE001 — a stubborn connector never blocks shutdown
                    log.warning("trigger %r stop failed", getattr(trigger, "name", "?"), exc_info=True)
        await self._scheduler.stop()
        self._shutdown.set()
        log.info("runner stopped")

    async def serve_forever(self) -> None:
        """Start and block until a shutdown signal (SIGINT/SIGTERM), then stop."""
        await self.start()
        loop = asyncio.get_running_loop()
        import signal

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._shutdown.set)
            except (NotImplementedError, ValueError):  # e.g. Windows / non-main thread
                pass
        try:
            await self._shutdown.wait()
        finally:
            await self.stop()

    # -- dispatch ---------------------------------------------------------------

    async def _consume(self, trigger: EventTrigger) -> None:
        try:
            async for event in trigger.stream():
                await self._dispatch_event(trigger, event)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a broken stream shouldn't kill the runner
            log.exception("event stream %r failed", getattr(trigger, "name", "?"))

    async def _dispatch_event(self, trigger: EventTrigger, event: InboundEvent) -> None:
        trig_desc = f"event:{event.source}/{event.kind}"
        matched = False
        for sub in list(self.registry.event_subs):
            if sub.matches(event):
                matched = True
                await self._run(sub.name, trig_desc, lambda s=sub: s.handler(event))
        if matched:
            # Idempotency hook: a connector can mark the event processed.
            try:
                await trigger.ack(event)
            except Exception:  # noqa: BLE001
                log.warning("ack failed for %s", trig_desc, exc_info=True)

    def _schedule_job(self, name: str, handler: Callable[[], Awaitable]) -> Callable[[], Awaitable[None]]:
        async def job() -> None:
            await self._run(name, f"schedule:{name}", handler)
        return job

    async def _run(self, sub_name: str, trigger_desc: str, work: Callable[[], Awaitable]) -> RunRecord:
        rec = RunRecord(
            id=uuid.uuid4().hex[:12], subscription=sub_name, trigger=trigger_desc,
            status="ok", started_at=_now(), ended_at="",
        )
        try:
            await work()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — resilience: never propagate
            rec.status = "error"
            rec.error = f"{type(exc).__name__}: {exc}"
            log.error("run failed [%s on %s]: %s", sub_name, trigger_desc, rec.error)
        rec.ended_at = _now()
        self._record(rec)
        return rec

    def _record(self, rec: RunRecord) -> None:
        self.records.append(rec)
        if len(self.records) > self.max_records:
            del self.records[: len(self.records) - self.max_records]
        if self.observer is not None:
            try:
                self.observer(rec)
            except Exception:  # noqa: BLE001 — observers never break the runner
                log.warning("run observer raised", exc_info=True)
