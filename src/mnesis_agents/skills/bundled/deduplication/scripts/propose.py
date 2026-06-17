#!/usr/bin/env python3
"""Deterministic proposal builder for the deduplication skill.

Reads near-duplicate candidates as JSON (file path argument, or stdin) and emits
**merge/supersession proposals** for the strong pairs. It performs NO writes — it
cannot merge or supersede anything; it only filters, ranks, and explains. The
propose-only policy is structural: this script never calls a tool.

Strong pairs (similarity >= strong_threshold) become proposals; weaker pairs are
reported under ``skipped_weak`` so nothing silently vanishes. When per-page
confidence is supplied, the higher-confidence page is recommended as ``keep``;
otherwise ``keep`` stays null for a human to decide.
"""
from __future__ import annotations

import json
import sys

_DEFAULT_STRONG = 0.5


def _load() -> dict:
    raw = open(sys.argv[1], encoding="utf-8").read() if len(sys.argv) > 1 else sys.stdin.read()
    return json.loads(raw or "{}")


def _choose_keep(a: str, b: str, pages: dict) -> tuple[str | None, str | None]:
    """(keep, supersede) by confidence if both known, else (None, None)."""
    ca = pages.get(a, {}).get("confidence")
    cb = pages.get(b, {}).get("confidence")
    if ca is None or cb is None:
        return None, None
    return (a, b) if ca >= cb else (b, a)


def main() -> int:
    data = _load()
    threshold = float(data.get("strong_threshold", _DEFAULT_STRONG))
    pages = data.get("pages", {})

    proposals, skipped = [], []
    for c in data.get("candidates", []):
        sim = float(c.get("similarity", 0.0))
        a, b = c["page_a"], c["page_b"]
        if sim < threshold:
            skipped.append({"page_a": a, "page_b": b, "similarity": round(sim, 3)})
            continue
        keep, supersede = _choose_keep(a, b, pages)
        keep_clause = (
            f"Keep {keep}, supersede {supersede} (higher confidence)."
            if keep
            else "Confidence unknown — a human picks which page to keep."
        )
        proposals.append({
            "page_a": a,
            "page_b": b,
            "proposed_action": "supersede",
            "keep": keep,
            "supersede": supersede,
            "similarity": round(sim, 3),
            "rationale": f"strong near-duplicate (similarity {sim:.2f}): {c.get('rationale', '')}. {keep_clause}",
        })

    proposals.sort(key=lambda p: (-p["similarity"], p["page_a"], p["page_b"]))
    out = {
        "skill": "deduplication",
        "action": "propose",
        "auto_apply": False,
        "proposals": proposals,
        "skipped_weak": skipped,
        "note": "PROPOSALS ONLY — nothing was merged or superseded. Heuristic; pending Phase-5 vectors.",
    }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
