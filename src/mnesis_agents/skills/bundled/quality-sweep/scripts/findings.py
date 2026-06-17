#!/usr/bin/env python3
"""Deterministic finding builder for the quality-sweep skill.

Reads the JSON from ``mnesis_health_report`` (file path argument, or stdin) and
categorises it into prioritised, read-only findings. Performs NO writes and
proposes nothing — it only shapes observations. Pure and deterministic.
"""
from __future__ import annotations

import json
import sys


def _load() -> dict:
    raw = open(sys.argv[1], encoding="utf-8").read() if len(sys.argv) > 1 else sys.stdin.read()
    return json.loads(raw or "{}")


def main() -> int:
    r = _load()
    findings: list[dict] = []

    no_sources = r.get("no_sources", [])
    if no_sources:
        findings.append({
            "type": "no_source_pages", "severity": "high", "count": len(no_sources),
            "page_ids": no_sources, "detail": "Pages with no source — unverifiable; ingest supporting material.",
        })

    low_conf = r.get("low_confidence_pages", [])
    if low_conf:
        findings.append({
            "type": "low_confidence", "severity": "medium", "count": len(low_conf),
            "page_ids": low_conf, "detail": "Pages below the stale threshold; reinforce with fresh sources.",
        })

    orphans = int(r.get("orphan_entities", 0))
    if orphans:
        findings.append({
            "type": "orphan_entities", "severity": "low", "count": orphans,
            "detail": "Entities declared as tags but in no edge — consider relating or removing.",
        })

    undeclared = int(r.get("undeclared_entities", 0))
    if undeclared:
        findings.append({
            "type": "undeclared_entities", "severity": "low", "count": undeclared,
            "detail": "Entities used in edges but not declared as a tag — run graph-hygiene to flag.",
        })

    if int(r.get("open_contradictions", 0)):
        findings.append({
            "type": "open_contradictions", "severity": "high",
            "count": int(r["open_contradictions"]),
            "detail": "Unresolved contradictions — run contradiction-triage to adjudicate.",
        })

    index = r.get("index", {})
    if index and not index.get("fresh", True):
        findings.append({
            "type": "index_stale", "severity": "high",
            "detail": "Search index out of sync with Markdown — run `mnesis rebuild`.",
        })
    graph_index = r.get("graph_index", {})
    if graph_index and not graph_index.get("fresh", True):
        findings.append({
            "type": "graph_cache_stale", "severity": "high",
            "detail": "Graph cache out of sync with Markdown — run `mnesis rebuild`.",
        })

    severity_rank = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: severity_rank.get(f["severity"], 9))

    out = {
        "skill": "quality-sweep",
        "action": "report",
        "auto_apply": False,
        "findings": findings,
        "summary": {
            "pages_total": r.get("pages_total"),
            "stale": r.get("stale"),
            "open_contradictions": r.get("open_contradictions"),
        },
        "note": "Read-only findings; nothing was changed or proposed.",
    }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
