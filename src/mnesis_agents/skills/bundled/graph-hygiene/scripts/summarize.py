#!/usr/bin/env python3
"""Deterministic post-processing for the graph-hygiene skill.

Reads JSON ``{"report": <mnesis_graph_lint fix=False>, "applied": <fix=True>}``
(from a file path argument, or stdin) and emits the documented structured
summary: what was auto-fixed (safe categories) and what is flagged for a human.
Pure and deterministic; the safe fixes were already applied on the Mnesis side.
"""
from __future__ import annotations

import json
import sys

_SAFE = ("duplicate_edges", "stale_only_edges", "confidence_updates")


def _load() -> dict:
    raw = open(sys.argv[1], encoding="utf-8").read() if len(sys.argv) > 1 else sys.stdin.read()
    return json.loads(raw or "{}")


def _fixed_counts(applied: dict, report: dict) -> dict:
    """Counts of the safe categories that were fixed — prefer the fix=True result,
    fall back to what the report said was fixable."""
    src = applied.get("fixed_categories") or report.get("fixable_categories") or {}
    counts = {cat: int(src.get(cat, 0)) for cat in _SAFE}
    counts["total"] = sum(counts.values())
    return counts


def main() -> int:
    data = _load()
    report = data.get("report", {})
    applied = data.get("applied", {})

    fixed = _fixed_counts(applied, report)
    # Flagged items: prefer the structured list from either lint result.
    flagged = applied.get("flagged_items") or report.get("flagged_items") or []

    out = {
        "skill": "graph-hygiene",
        "action": "auto_applied",
        "auto_apply": True,
        "fixed": fixed,
        "flagged_for_human": flagged,
        "message": (
            f"Graph hygiene: applied {fixed['total']} safe fix(es); "
            f"{len(flagged)} item(s) flagged for human review."
        ),
        "note": "Only safe categories were auto-fixed; flagged items change meaning and need a human.",
    }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
