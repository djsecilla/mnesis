"""MaintenanceAgent — the curation / "dream cycle" category.

Trigger: a **schedule**. The agent reads and curates Mnesis and its graph —
orchestrating decay, graph-lint and consolidation via Mnesis tools — and
proposes changes under governance (write policy ``propose``). Concrete subclasses
declare their cadence and scope; nothing is auto-applied destructively here.
"""
from __future__ import annotations

from abc import abstractmethod
from typing import ClassVar

from .base import CategoryAgent


class MaintenanceAgent(CategoryAgent):
    """Abstract base for scheduled curation agents.

    Concrete subclasses must declare ``cadence`` (how often the dream cycle runs)
    and ``scope`` (what it is allowed to touch — e.g. which maintenance ops /
    page selections), so a scheduler (F-later) can run them safely.
    """

    category: ClassVar[str] = "maintenance"
    trigger: ClassVar[str] = "schedule"
    write_policy: ClassVar[str] = "propose"

    @abstractmethod
    def cadence(self) -> str:
        """How often this runs (e.g. a cron expression or interval like '1d')."""

    @abstractmethod
    def scope(self) -> list[str]:
        """What this maintenance run may touch (maintenance ops / page selectors)."""
