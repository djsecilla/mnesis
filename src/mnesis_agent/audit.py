"""Append-only JSONL run audit.

One JSONL file per UTC day under a configurable directory. Records:

  run_start  {run_id, ts, type, profile, input}
  step       {run_id, ts, type, kind: thought|tool, ...}      (one per loop step)
  run_end    {run_id, ts, type, stop_reason, iterations, usage,
              tools_used, writes:[{tool, call_id}], citation_count, citations}

**Never logged:** tool argument *values*, tool result *bodies*, or any redacted
secret/PII value. Step records carry only ``args_keys`` (sorted key names) and a
``status`` (ok | error) — these come from the loop's audit hook (A3), which is
redaction-safe by construction. Writes are logged as tool name + call id only.
Page ids and citations are non-secret identifiers and are recorded.

The user ``input`` (the run's question/goal) is recorded as given — it is the
run's own prompt, not a tool payload. It is truncated to a bounded preview so
the log cannot grow without limit.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import config

#: Hard cap on the stored input preview (chars).
_INPUT_PREVIEW_MAX = 500


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_run_id() -> str:
    """A short, unique run identifier."""
    return uuid.uuid4().hex[:16]


@dataclass
class AuditLog:
    """Append-only JSONL audit writer.

    Thread-/process-safe enough for the PoC: each record is a single
    ``open(..., 'a')`` + write + close, which is atomic for small lines on
    POSIX. ``directory`` is created on demand.
    """

    directory: Path

    def __post_init__(self) -> None:
        self.directory = Path(self.directory)

    # ── low-level append ──────────────────────────────────────────────────────

    def _path_for_today(self) -> Path:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self.directory / f"runs-{day}.jsonl"

    def _append(self, record: dict) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False)
        path = self._path_for_today()
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    # ── run lifecycle ─────────────────────────────────────────────────────────

    def start_run(self, profile: str, user_input: str, *, run_id: str | None = None) -> str:
        """Write the run_start record; returns the run id."""
        run_id = run_id or new_run_id()
        self._append({
            "run_id": run_id,
            "ts": _now_iso(),
            "type": "run_start",
            "profile": profile,
            "input": user_input[:_INPUT_PREVIEW_MAX],
            "input_len": len(user_input),
        })
        return run_id

    def step_hook(self, run_id: str) -> Callable[[dict], None]:
        """Return a callable suitable as the loop's ``audit_hook``.

        The loop emits already-redacted step dicts (thought: text_length only;
        tool: args_keys + status). We stamp them with run_id/ts and append.
        """
        def _hook(step: dict) -> None:
            rec = {"run_id": run_id, "ts": _now_iso(), "type": "step", **step}
            self._append(rec)
        return _hook

    def record_refusal(self, run_id: str, tool: str) -> None:
        """Record a policy refusal (tool name only — no args, no values)."""
        self._append({
            "run_id": run_id,
            "ts": _now_iso(),
            "type": "step",
            "kind": "refusal",
            "tool": tool,
            "status": "refused",
        })

    def end_run(self, run_id: str, result) -> None:
        """Write the run_end summary from a (Grounded)AgentResult."""
        self._append({
            "run_id": run_id,
            "ts": _now_iso(),
            "type": "run_end",
            "stop_reason": result.stop_reason,
            "iterations": result.iterations,
            "usage": result.usage,
            "tools_used": result.tools_used,
            # writes: tool name + call id only — never args/values
            "writes": [{"tool": w.name, "call_id": w.id} for w in result.writes],
            "citation_count": len(result.citations),
            "citations": list(result.citations),  # page ids (non-secret)
        })


def default_audit_log() -> AuditLog:
    """Construct an AuditLog at the configured directory."""
    return AuditLog(config.MNESIS_AGENT_AUDIT_DIR)


def read_run_records(directory: Path | str, run_id: str) -> list[dict]:
    """Read all records for a run id across the directory's JSONL files.

    Convenience for inspection/tests. Returns records in file order.
    """
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
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("run_id") == run_id:
                    out.append(rec)
    return out
