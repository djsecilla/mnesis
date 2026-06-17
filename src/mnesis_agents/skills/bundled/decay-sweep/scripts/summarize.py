#!/usr/bin/env python3
"""Deterministic post-processing for the decay-sweep skill.

Reads the JSON transition counts produced by ``mnesis_decay`` (from a file path
argument, or stdin) and emits the skill's documented structured summary. Pure,
deterministic, no side effects — the decay write already happened on the Mnesis
side; this only shapes the report.
"""
from __future__ import annotations

import json
import sys


def _load() -> dict:
    raw = open(sys.argv[1], encoding="utf-8").read() if len(sys.argv) > 1 else sys.stdin.read()
    return json.loads(raw or "{}")


def main() -> int:
    data = _load()
    counts = {k: int(data.get(k, 0)) for k in ("scanned", "restaled", "reactivated", "unchanged")}
    message = (
        f"Decay sweep complete: {counts['restaled']} went stale, "
        f"{counts['reactivated']} reactivated, {counts['unchanged']} unchanged "
        f"of {counts['scanned']} scanned."
    )
    out = {
        "skill": "decay-sweep",
        "action": "auto_applied",
        "auto_apply": True,
        "summary": counts,
        "message": message,
        "note": "Safe hygiene applied automatically; no knowledge meaning changed.",
    }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
