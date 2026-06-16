"""Schedule triggers — periodic firing for maintenance dream-cycles and reminders.

The ``Schedule`` shape carries either an interval or a cron expression. The
bundled :class:`AsyncIntervalScheduler` is a dependency-free asyncio scheduler
that supports **interval** scheduling; cron scheduling is declared on the shape
but requires APScheduler (an optional extra), which a later prompt can wire in.
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Schedule:
    """How often something fires: an interval (seconds) or a cron expression."""

    interval_seconds: float | None = None
    cron: str | None = None

    def __post_init__(self) -> None:
        if self.interval_seconds is None and self.cron is None:
            raise ValueError("Schedule needs interval_seconds or cron")

    def describe(self) -> str:
        if self.interval_seconds is not None:
            return f"every {self.interval_seconds}s"
        return f"cron({self.cron})"


class ScheduleTrigger(ABC):
    """An agent that fires on a schedule declares it here."""

    @abstractmethod
    def schedule(self) -> Schedule:
        """The cadence at which this trigger fires."""


class AsyncIntervalScheduler:
    """A minimal asyncio scheduler: fires async callbacks on a fixed interval.

    Interval only — a Schedule with ``cron`` raises a clear error (cron support
    is a future APScheduler-backed extension). Callbacks are awaited; a raising
    callback is swallowed so one bad job never kills the scheduler.
    """

    def __init__(self) -> None:
        self._jobs: list[tuple[str, float, Callable[[], Awaitable[None]]]] = []
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()

    def add_job(self, name: str, schedule: Schedule, callback: Callable[[], Awaitable[None]]) -> None:
        if schedule.interval_seconds is None:
            raise ValueError(
                f"AsyncIntervalScheduler supports interval schedules only; "
                f"{schedule.describe()} needs APScheduler (not bundled)."
            )
        self._jobs.append((name, schedule.interval_seconds, callback))

    async def start(self) -> None:
        self._stop.clear()
        self._tasks = [
            asyncio.create_task(self._loop(name, interval, cb))
            for name, interval, cb in self._jobs
        ]

    async def _loop(self, name: str, interval: float, cb: Callable[[], Awaitable[None]]) -> None:
        while not self._stop.is_set():
            try:
                # Sleep one interval, but wake immediately if stop is signaled.
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
                return  # stop requested
            except asyncio.TimeoutError:
                pass  # interval elapsed -> fire
            try:
                await cb()
            except Exception:  # noqa: BLE001 — a bad job must not kill the loop
                pass  # the runner's callback already records errors

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
