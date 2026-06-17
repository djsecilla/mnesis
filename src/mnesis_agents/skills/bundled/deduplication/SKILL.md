---
name: deduplication
description: Find near-duplicate Mnesis pages and PROPOSE a merge/supersession for the strong candidates, with a rationale. Proposal-only — it NEVER applies a change. Use to surface redundant knowledge for review. Heuristic, pending Phase-5 vectors.
version: 0.1.0
license: MIT
allowed-tools:
  - mnesis_find_duplicates
  - mnesis_get
---
# Deduplication (PROPOSE, do not apply)

Redundant pages dilute confidence and clutter retrieval. Mnesis's
`mnesis_find_duplicates` surfaces near-duplicate candidate pairs by a heuristic
(title/tag overlap, shared graph edges, FTS co-retrieval). This routine turns the
strong candidates into **merge/supersession proposals**.

## Policy — PROPOSE ONLY (never writes)

This routine **MUST only propose**. Merging or superseding a page changes what
the knowledge base asserts and removes a page from active retrieval — a meaning
change, never hygiene. This skill is **not** granted any write tool (no
`mnesis_ingest`, no supersession); knowledge changes only by ingesting a
reconciling source or via an explicit, human-confirmed supersession later. The
underlying duplicate finder is itself read-only and proposes nothing.

> The duplicate finder is a **heuristic stand-in pending Phase-5 vectors**, so
> treat every candidate as a suggestion to verify, not a fact.

## Procedure

1. Call `mnesis_find_duplicates` to get candidate pairs with their similarity and
   signals.
2. Keep only the **strong** candidates (similarity ≥ the strong threshold, default
   `0.5`); weaker pairs are reported as "skipped" so nothing silently disappears.
3. Optionally call `mnesis_get` on each page of a strong pair to read confidence;
   when both are known, recommend keeping the higher-confidence page. When they
   are not known, leave `keep` null — the human/agent decides.
4. Hand the candidates (plus any per-page confidence gathered) to
   `scripts/propose.py <file>`, then present the proposals. Make clear nothing was
   merged or superseded.

Input the script expects:

```json
{"candidates": [ … from mnesis_find_duplicates … ],
 "strong_threshold": 0.5,
 "pages": {"atlas-redis": {"confidence": 0.82}, "atlas-redis-cache": {"confidence": 0.50}}}
```

## Structured output

`scripts/propose.py` emits **proposals only**:

```json
{
  "skill": "deduplication",
  "action": "propose",
  "auto_apply": false,
  "proposals": [
    {"page_a": "atlas-redis", "page_b": "atlas-redis-cache",
     "proposed_action": "supersede", "keep": "atlas-redis", "supersede": "atlas-redis-cache",
     "similarity": 0.62,
     "rationale": "strong near-duplicate (similarity 0.62): shared tags 1.00; shared edges 1.00. Keep higher-confidence page."}
  ],
  "skipped_weak": [{"page_a": "pg-backups", "page_b": "pg-restore", "similarity": 0.28}],
  "note": "PROPOSALS ONLY — nothing was merged or superseded. Heuristic; pending Phase-5 vectors."
}
```
