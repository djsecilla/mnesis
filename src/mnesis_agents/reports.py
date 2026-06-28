"""Persisting and summarising dream-cycle reports.

A :class:`DreamReportStore` persists each :class:`DreamCycleReport` (full JSON,
append-only history) under the agents' artefact dir and exposes the **latest**
one — for the CLI ``mnesis-agents dream-cycle --report`` and the runner. It also
writes a human-readable summary (the latest, as plain text) and mirrors a compact
record into the **F6 audit log** (names / statuses / ids / counts only).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import config
from .audit import AgentAuditLog, new_run_id
from .config import now_iso as _now

if TYPE_CHECKING:
    from .maintenance_agent import DreamCycleReport


def _pages(health: dict | None) -> Any:
    return health.get("pages_total") if isinstance(health, dict) else None


def format_summary(report: "DreamCycleReport") -> str:
    """A concise, human-readable summary of a dream cycle (no secrets/PII —
    counts, statuses, page/review ids only)."""
    t = report.totals
    lines = [
        f"Dream cycle  {report.started} → {report.ended}",
        f"  passes: {t.get('passes', 0)}  "
        f"(ok {t.get('ok', 0)}, failed {t.get('failed', 0)}, skipped {t.get('skipped', 0)})",
        f"  auto-applied: {t.get('auto_applied', 0)}   proposals: {t.get('proposals', 0)}",
        f"  health pages: {_pages(report.health_before)} → {_pages(report.health_after)}",
        f"  stop_reason: {t.get('stop_reason')}",
    ]
    if t.get("crystallized_digest_id"):
        lines.append(f"  crystallized digest: {t['crystallized_digest_id']}")
    for p in report.passes:
        mark = {"ok": "✓", "failed": "✗", "skipped": "·"}.get(p.status, "?")
        extra = ""
        if p.auto_applied:
            extra += f"  auto-applied {len(p.auto_applied)}"
        if p.proposals:
            extra += f"  proposals {len(p.proposals)}"
        if p.error:
            extra += f"  error: {p.error}"
        lines.append(f"    {mark} {p.name}{extra}")
    return "\n".join(lines)


class DreamReportStore:
    """Persists dream-cycle reports (JSONL history) + the latest human summary."""

    def __init__(
        self,
        directory: Path | str | None = None,
        *,
        filename: str = "dream-cycles.jsonl",
        audit: AgentAuditLog | None = None,
    ) -> None:
        self.directory = Path(directory or config.MNESIS_AGENTS_PROPOSALS_DIR)
        self._path = self.directory / filename
        self._summary_path = self.directory / "dream-cycle.latest.txt"
        self._audit = audit or AgentAuditLog(self.directory)

    def save(self, report: "DreamCycleReport") -> str:
        """Persist a report (full JSON + latest summary) and mirror it into the
        audit log. Returns the audit ``run_id``."""
        self.directory.mkdir(parents=True, exist_ok=True)
        run_id = new_run_id()
        record = {"run_id": run_id, "saved_at": _now(),
                  "report": report.to_dict()}
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._summary_path.write_text(format_summary(report), encoding="utf-8")
        self._audit.write_dream_cycle(report, run_id=run_id)
        return run_id

    def latest(self) -> dict[str, Any] | None:
        """The most recently saved report dict, or None if none has run."""
        if not self._path.is_file():
            return None
        last = None
        with open(self._path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    last = json.loads(line)
        return last["report"] if last else None

    def latest_summary(self) -> str | None:
        """The latest cycle's human-readable summary, or None if none has run."""
        if self._summary_path.is_file():
            return self._summary_path.read_text(encoding="utf-8")
        return None
