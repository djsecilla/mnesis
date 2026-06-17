---
name: contradiction-triage
description: Review open Mnesis contradictions and PROPOSE which page to keep — ranked by confidence, source count, and recency — with a rationale. Proposal-only — it NEVER resolves a contradiction. Use when the review queue needs adjudication.
version: 0.1.0
license: MIT
allowed-tools:
  - mnesis_review
  - mnesis_get
---
# Contradiction triage (PROPOSE, do not resolve)

When two pages contradict each other and there is no clear confidence winner,
Mnesis files a review instead of auto-resolving. This routine adjudicates those
reviews into **proposals** for a human (or a higher-authority agent) to apply.

## Policy — PROPOSE ONLY (never writes)

This routine **MUST only propose**. Choosing which page survives a contradiction
changes what the knowledge base *asserts* — that is a meaning change, never
hygiene — so it is out of policy to auto-apply. This skill is **not** granted the
`mnesis_resolve` tool; it emits proposals and stops. A human applies a proposal
later with `mnesis resolve <review_id> --keep <page_id>` (or the `mnesis_resolve`
tool under a write-enabled profile).

## Procedure

1. Call `mnesis_review` to list the open contradictions. For each, note the two
   page ids, their current confidence, and the conflict detail.
2. For each contradicting page, call `mnesis_get` to read its `source_count` and
   `last_confirmed` (the recency clock). (The fake/stub review tool already
   bundles these fields for offline testing.)
3. Assemble the gathered data as JSON and hand it to `scripts/triage.py <file>`,
   which ranks each pair deterministically and proposes a `keep`/`supersede`.
   Ranking precedence: **higher confidence**, then **more sources**, then **more
   recently confirmed**.
4. Present the proposals with their rationale. Make clear nothing was resolved.

Input the script expects:

```json
{"contradictions": [
  {"review_id": 1, "detail": "…",
   "pages": [
     {"id": "p1", "confidence": 0.82, "source_count": 3, "last_confirmed": "2026-06-10T12:00:00Z"},
     {"id": "p2", "confidence": 0.45, "source_count": 1, "last_confirmed": "2026-02-01T12:00:00Z"}
   ]}
]}
```

## Structured output

`scripts/triage.py` emits **proposals only**:

```json
{
  "skill": "contradiction-triage",
  "action": "propose",
  "auto_apply": false,
  "proposals": [
    {"review_id": 1, "keep": "p1", "supersede": "p2",
     "confidence_gap": 0.37, "strength": "strong",
     "rationale": "keep p1 (confidence 0.82 vs 0.45; sources 3 vs 1; more recently confirmed)"}
  ],
  "note": "PROPOSALS ONLY — no contradictions were resolved. Apply with mnesis_resolve after review."
}
```
