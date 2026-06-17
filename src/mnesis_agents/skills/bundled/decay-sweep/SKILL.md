---
name: decay-sweep
description: Run the Mnesis decay/lifecycle pass to age knowledge — recompute confidence and transition pages between active and stale. Safe hygiene that AUTO-APPLIES. Use for scheduled upkeep of knowledge freshness (the "dream cycle").
version: 0.1.0
license: MIT
allowed-tools:
  - mnesis_decay
---
# Decay sweep (dream-cycle hygiene)

Let knowledge fade and recover gracefully. This routine runs Mnesis's
decay/lifecycle pass, which recomputes every page's confidence and moves pages
between `active` and `stale` based on age, reads, and reinforcement.

## Policy — AUTO-APPLY (safe)

This routine **MAY auto-apply**. The decay pass is pure hygiene: it never changes
the *meaning* of any knowledge, only the confidence-driven `active`↔`stale`
status, and every transition is one idempotent, git-audited commit on the Mnesis
side. Running it twice with no time passing changes nothing. There is nothing to
propose — just run it and report.

## Procedure

1. Call `mnesis_decay` (no arguments). It returns the transition counts:
   `scanned`, `restaled` (active→stale), `reactivated` (stale→active), and
   `unchanged`.
2. Assemble those counts into a JSON object
   `{"scanned": N, "restaled": N, "reactivated": N, "unchanged": N}` and hand it
   to `scripts/summarize.py <file>` for the deterministic, structured summary.
3. Report the summary. Call out anything notable (e.g. a large number of pages
   going stale at once may indicate a corpus that has not been reinforced).

## Structured output

`scripts/summarize.py` emits:

```json
{
  "skill": "decay-sweep",
  "action": "auto_applied",
  "auto_apply": true,
  "summary": {"scanned": 6, "restaled": 1, "reactivated": 0, "unchanged": 5},
  "message": "Decay sweep complete: 1 went stale, 0 reactivated, 5 unchanged of 6 scanned.",
  "note": "Safe hygiene applied automatically; no knowledge meaning changed."
}
```
