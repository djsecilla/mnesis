"""Append-only JSONL run audit for the agentic layer.

One JSONL file per UTC day. Records: ``run_start`` → one ``step`` per agent step
(model turn, each tool call, each skill activation, each interrupt/approval) →
``run_end``. **Never logs argument values, tool results, message content, or any
secret/PII** — only tool/skill *names*, statuses, ids, and counts.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_run_id() -> str:
    return uuid.uuid4().hex[:16]


class AgentAuditLog:
    """Append-only JSONL writer (one file per UTC day)."""

    def __init__(self, directory: Path | str | None = None) -> None:
        self.directory = Path(directory or config.MNESIS_AGENTS_AUDIT_DIR)

    def _path(self) -> Path:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self.directory / f"runs-{day}.jsonl"

    def _append(self, record: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        with open(self._path(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def write_run(
        self,
        *,
        run_id: str,
        category: str,
        trigger: str,
        profile: str,
        result,
        interrupted: bool = False,
    ) -> None:
        """Emit run_start + one record per step + run_end from an AgentResult.

        Steps are derived from the result's messages: a ``model`` step per AI
        turn, a ``tool``/``skill`` step per tool call (skills are use_skill calls),
        each with a status — names and statuses only, no values.
        """
        self._append({
            "run_id": run_id, "ts": _now(), "type": "run_start",
            "category": category, "trigger": trigger, "profile": profile,
        })

        # Map tool_call_id -> ToolMessage status to label each tool step.
        statuses: dict[str, str] = {}
        for m in result.messages:
            if getattr(m, "type", None) == "tool":
                statuses[getattr(m, "tool_call_id", "")] = getattr(m, "status", "ok") or "ok"

        for m in result.messages:
            mtype = getattr(m, "type", None)
            tool_calls = getattr(m, "tool_calls", None) or []
            if mtype == "ai":
                self._append({"run_id": run_id, "ts": _now(), "type": "step", "kind": "model"})
            for tc in tool_calls:
                name = tc.get("name", "")
                tcid = tc.get("id", "")
                bare = name.split("__", 1)[-1]
                if bare == "use_skill":
                    self._append({
                        "run_id": run_id, "ts": _now(), "type": "step", "kind": "skill",
                        "skill": (tc.get("args") or {}).get("name"),
                        "status": statuses.get(tcid, "ok"),
                    })
                else:
                    self._append({
                        "run_id": run_id, "ts": _now(), "type": "step", "kind": "tool",
                        "tool": name, "status": statuses.get(tcid, "ok"),
                    })

        self._append({
            "run_id": run_id, "ts": _now(), "type": "run_end",
            "stop_reason": result.stop_reason,
            "steps": result.steps,
            "tools_used": result.tools_used,
            "skills_used": result.skills_used,
            "writes": [{"tool": w.get("tool")} for w in result.writes],
            "refusals": [{"tool": r.get("tool"), "reason": r.get("reason")} for r in result.refusals],
            "interrupted": interrupted,
            "usage": result.usage,
        })


def read_run_records(directory: Path | str, run_id: str) -> list[dict]:
    """Read all records for a run id across the audit dir (for inspection/tests)."""
    directory = Path(directory)
    out: list[dict] = []
    if not directory.is_dir():
        return out
    for fname in sorted(os.listdir(directory)):
        if not fname.endswith(".jsonl"):
            continue
        with open(directory / fname, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rec = json.loads(line)
                    if rec.get("run_id") == run_id:
                        out.append(rec)
    return out
