#!/usr/bin/env python3
"""Deterministic adjudication for the contradiction-triage skill.

Reads the gathered contradiction data as JSON (file path argument, or stdin) and
emits a **proposal** of which page to keep for each open review. It performs NO
writes — it cannot resolve anything; it only ranks and explains. The propose-only
policy is structural: this script never calls a tool.

Ranking precedence (deterministic): higher confidence, then more sources, then
more recently confirmed (later ``last_confirmed``).
"""
from __future__ import annotations

import json
import sys

# A confidence gap at/above this is a "strong" recommendation; below it, the
# pages are close and a human should look harder. Mirrors Mnesis's
# AUTO_RESOLVE_MARGIN — but here it only labels the proposal, never auto-applies.
_STRONG_GAP = 0.25


def _load() -> dict:
    raw = open(sys.argv[1], encoding="utf-8").read() if len(sys.argv) > 1 else sys.stdin.read()
    return json.loads(raw or "{}")


def _rank_key(page: dict) -> tuple:
    return (
        float(page.get("confidence", 0.0)),
        int(page.get("source_count", 0)),
        str(page.get("last_confirmed", "")),
    )


def _proposal(review: dict) -> dict | None:
    pages = review.get("pages", [])
    if len(pages) < 2:
        return None
    ordered = sorted(pages, key=_rank_key, reverse=True)
    keep, drop = ordered[0], ordered[1]
    gap = round(float(keep.get("confidence", 0.0)) - float(drop.get("confidence", 0.0)), 4)
    rationale = (
        f"keep {keep['id']} (confidence {keep.get('confidence', 0):.2f} vs "
        f"{drop.get('confidence', 0):.2f}; sources {keep.get('source_count', 0)} vs "
        f"{drop.get('source_count', 0)}; "
        + (
            "more recently confirmed"
            if str(keep.get("last_confirmed", "")) >= str(drop.get("last_confirmed", ""))
            else "older but otherwise stronger"
        )
        + ")"
    )
    return {
        "review_id": review.get("review_id"),
        "keep": keep["id"],
        "supersede": drop["id"],
        "confidence_gap": gap,
        "strength": "strong" if gap >= _STRONG_GAP else "weak — close call, prefer human review",
        "rationale": rationale,
    }


def main() -> int:
    data = _load()
    proposals = [p for r in data.get("contradictions", []) if (p := _proposal(r))]
    out = {
        "skill": "contradiction-triage",
        "action": "propose",
        "auto_apply": False,
        "proposals": proposals,
        "note": "PROPOSALS ONLY — no contradictions were resolved. Apply with mnesis_resolve after review.",
    }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
