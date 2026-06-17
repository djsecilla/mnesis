---
name: graph-hygiene
description: Lint the Mnesis knowledge graph and AUTO-APPLY only the safe fixes (merge duplicate edges, demote stale-only edges, recompute edge confidence); flag everything else (undeclared/orphan entities, dangling structural edges) for human review. Use for scheduled graph upkeep.
version: 0.1.0
license: MIT
allowed-tools:
  - mnesis_graph_lint
---
# Graph hygiene (dream-cycle graph upkeep)

Keep the typed knowledge graph consistent. Mnesis's graph lint splits issues into
**safe, deterministic auto-fixes** and **items that need a human**. This routine
applies only the former and surfaces the latter — it never invents or deletes
knowledge.

## Policy — AUTO-APPLY safe fixes only

This routine **MAY auto-apply the safe categories only**:

- merge duplicate `(s, p, o)` edges,
- demote stale-only edges (every supporting page is stale/superseded),
- recompute each edge's noisy-OR confidence from its pages' current confidence.

It **MUST NOT** act on the flagged categories — undeclared entities, orphan
entities, dangling structural edges — which can change what the graph *means*.
Those are emitted as findings for a human. The fix is idempotent and never
deletes an edge that still has an active supporting page.

## Procedure

1. Call `mnesis_graph_lint(fix=False)` to get the current report (what is fixable
   and what is flagged).
2. Call `mnesis_graph_lint(fix=True)` to apply the safe auto-fixes. (It is
   idempotent — safe to run even if the report showed nothing.)
3. Assemble both results as JSON `{"report": <fix=False result>, "applied":
   <fix=True result>}` and hand it to `scripts/summarize.py <file>` for the
   structured summary.
4. Report what was auto-fixed and, prominently, anything flagged for human review.

## Structured output

`scripts/summarize.py` emits:

```json
{
  "skill": "graph-hygiene",
  "action": "auto_applied",
  "auto_apply": true,
  "fixed": {"duplicate_edges": 1, "stale_only_edges": 0, "confidence_updates": 1, "total": 2},
  "flagged_for_human": [
    {"category": "undeclared_entities", "ref": "library:redis", "detail": "..."}
  ],
  "message": "Graph hygiene: applied 2 safe fix(es); 1 item flagged for human review.",
  "note": "Only safe categories were auto-fixed; flagged items change meaning and need a human."
}
```
