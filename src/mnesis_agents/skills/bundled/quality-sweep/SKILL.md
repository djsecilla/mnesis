---
name: quality-sweep
description: Read-only Mnesis health audit — flag no-source pages, low-confidence clusters, orphan/undeclared graph entities, open contradictions, and search/graph cache staleness. Findings only; it changes and proposes nothing. Use for a periodic state-of-the-knowledge report.
version: 0.1.0
license: MIT
allowed-tools:
  - mnesis_health_report
---
# Quality sweep (read-only findings)

A periodic state-of-the-knowledge audit. It reads Mnesis's health report and
turns it into prioritised findings, so drift (ungrounded pages, decaying
clusters, a graph that has fallen out of sync) is visible at a glance.

## Policy — READ-ONLY (no writes, no proposals)

This routine is **strictly read-only**. It calls one side-effect-free tool and
reports. It proposes no merges and resolves no contradictions — those belong to
the `deduplication` and `contradiction-triage` routines. Findings are
observations, not actions.

## Procedure

1. Call `mnesis_health_report` (no arguments). It returns counts by status/kind,
   pages with no sources, low-confidence and stale counts, the open-contradiction
   count, graph size with demoted/orphan/undeclared/dangling counts, and whether
   the search index and graph cache are in sync with the Markdown.
2. Hand the report JSON to `scripts/findings.py <file>` for deterministic
   categorisation into findings with a severity.
3. Report the findings, highest severity first. Suggest (do not perform) the
   follow-up routine for each: no-source/low-confidence → ingest more sources;
   contradictions → `contradiction-triage`; duplicates → `deduplication`; a stale
   cache → `mnesis rebuild`.

## Structured output

`scripts/findings.py` emits read-only findings:

```json
{
  "skill": "quality-sweep",
  "action": "report",
  "auto_apply": false,
  "findings": [
    {"type": "no_source_pages", "severity": "high", "count": 1,
     "page_ids": ["orphan-note"], "detail": "Pages with no source — unverifiable."},
    {"type": "low_confidence", "severity": "medium", "count": 2,
     "page_ids": ["old-fact", "transient-bug"], "detail": "Pages below the stale threshold."},
    {"type": "index_stale", "severity": "high", "detail": "Search index out of sync with Markdown."}
  ],
  "summary": {"pages_total": 7, "stale": 1, "open_contradictions": 1},
  "note": "Read-only findings; nothing was changed or proposed."
}
```
