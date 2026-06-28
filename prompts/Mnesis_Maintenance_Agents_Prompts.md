# Mnesis — Maintenance Agents (Dream Cycle) Build Playbook

**The first concrete agent family on the LangChain foundation: scheduled "dream-cycle" agents that continuously curate the knowledge layer and graph. A sequenced prompt set for Claude Code (Opus 4.8).**

Maintenance agents are Mnesis's equivalent of a sleep/consolidation cycle: on a schedule, they sweep the knowledge base — recomputing confidence and fading stale knowledge, keeping the graph healthy, triaging contradictions, and proposing merges — then report what they did and what needs a human. They sit on the agentic foundation (F1–F7), reach Mnesis only through MCP, and obey its governance.

Each maintenance routine is packaged as an **Agent Skill** (`SKILL.md`), loaded by progressive disclosure. The agent is the scheduled runtime that runs the skills under guardrails. Run this set after the foundation (F1–F7).

---

## Honest scope: what maps onto built tools

| Dream-cycle work | Built? | Here |
|---|---|---|
| Decay (confidence recompute, active↔stale) | Yes — `mnesis_decay` | Used directly. |
| Graph hygiene (orphans, stale-only/dangling edges, edge-confidence) | Yes — `graph-lint`, **CLI only** | M1 exposes it over MCP. |
| Contradiction triage | Yes — review queue (`mnesis_review`/`mnesis_resolve`) | Agent **proposes** resolutions; humans resolve. |
| Consolidation (memory tiers working→episodic→semantic) | **No** (not built in Phases 1–3) | Out of scope; instead a **heuristic near-duplicate finder** + **merge proposals** (stronger once Phase-5 vectors land). |
| Health/quality view | Partial | M1 adds a read-only `mnesis_health_report`. |

So this set delivers the curation that maps onto real tools, is candid about the dedup heuristic, and leaves true tier-consolidation for a later capability.

---

## Architecture decisions (read first)

1. **Maintenance routines are Agent Skills.** Each pass (decay-sweep, graph-hygiene, contradiction-triage, deduplication, quality-sweep) is a `SKILL.md` folder the agent loads by progressive disclosure. The dream cycle is declarative and extensible — adding a skill adds a pass.
2. **MCP-only — expose the missing operations first.** The agent reaches Mnesis solely via MCP tools, so M1 (a Mnesis-side change) exposes `graph-lint`, a health report, and a duplicate finder over the authenticated MCP endpoint before the agent uses them.
3. **Auto-apply only safe hygiene; propose everything that changes meaning.** Decay and safe graph-lint fixes auto-apply (idempotent, reversible). Contradiction resolutions and merges/supersessions are **proposed**, never auto-applied — they flow to the human review surface. Mnesis's own governance (redaction, review queue) still binds the agent.
4. **The dream cycle records itself.** Each run can crystallize a concise maintenance digest back into Mnesis — meta-memory: the system remembers its own curation.
5. **It replaces the deployment-level sidecar.** This in-app agent supersedes the D5 maintenance cron container; M5 retires it so maintenance isn't double-run.

---

## Picking up the seams

| Seam (as built) | The dream cycle uses it as |
|---|---|
| `mnesis_decay` (Phase 2) | The decay-sweep pass. |
| `graph-lint` (Phase 3, CLI) | Exposed over MCP (M1) → the graph-hygiene pass. |
| Contradiction review queue (Phase 2 / G11) | Where contradiction & merge proposals surface. |
| F3 Agent Skills subsystem | Loads the maintenance skills (progressive disclosure). |
| F4 MaintenanceAgent abstraction | The base the concrete agent extends. |
| F5 ScheduleTrigger + runner | Fires the nightly dream cycle. |
| F6 governance/budgets/audit/interrupts | Bounds and records every cycle. |
| D5 maintenance sidecar | Retired (M5) — the agent owns scheduled maintenance now. |

---

## Scope boundary

**In scope:** exposing maintenance ops over MCP · the maintenance Agent Skills · the concrete MaintenanceAgent dream-cycle graph · proposals/reporting/crystallization · scheduling · deployment + sidecar retirement.

**Out of scope (later):** true memory-tier consolidation · vector-based dedup (Phase 5) · action/writing agents · a dedicated proposals UI beyond reusing the existing review surface.

---

## Reusing the standard template & rules

Same six-part template — **CONTEXT / OBJECTIVE / BUILD / CONSTRAINTS / ACCEPTANCE / ON DONE** — and standing rules: offline-testable (fake model + fake Mnesis tools); conventional commits; self-checking acceptance; keep `CLAUDE.md`/README in sync; verify installed LangChain/LangGraph/mcp APIs. Keep **Opus 4.8** active. Prompts use the **M** prefix.

---

# The Prompts

---

## Prompt M1 — Expose maintenance operations over MCP (Mnesis side)

```
CONTEXT: The dream-cycle agent reaches Mnesis only via MCP, but some maintenance ops aren't MCP tools yet: graph-lint is CLI-only, and there's no health report or duplicate finder. Expose them so the agent can drive curation. This is a Mnesis-side change (the agent stays MCP-only).

OBJECTIVE: Add maintenance MCP tools to Mnesis - graph-lint, a health report, and a heuristic duplicate finder - and confirm decay/review/resolve/rebuild/graph-stats are registered.

BUILD:
- mnesis_graph_lint(fix: bool=False): wrap the existing Phase-3 graph-lint - report the categories; with fix=True apply only the safe auto-fixes (merge dupes, demote stale-only edges, recompute edge confidence). Return a structured report.
- mnesis_health_report(): read-only system health from existing store/graph/search/state functions - counts by status/kind, pages with no sources, low-confidence and stale counts, orphan entities, open-contradiction count, demoted-edge count, graph stats, and index/graph freshness vs Markdown. Cheap, side-effect-free.
- mnesis_find_duplicates(limit=20): read-only near-duplicate candidates via a heuristic (title/tag overlap, shared edges, FTS similarity). Returns candidate pairs with a similarity rationale. Proposes and changes nothing. Document clearly that this is heuristic pending Phase-5 vectors.
- Confirm mnesis_decay, mnesis_review, mnesis_resolve, mnesis_rebuild, mnesis_graph_stats are registered MCP tools; add any missing.

CONSTRAINTS:
- These live in the mnesis package / MCP server, behind the same authenticated endpoint.
- Read tools are strictly side-effect-free; the only writers are graph_lint(fix=True) and decay, both idempotent and git-audited.
- find_duplicates is heuristic and read-only - it proposes nothing.

ACCEPTANCE:
- Mnesis-side tests: graph_lint report/fix match the CLI; health_report returns the documented shape on a seeded corpus; find_duplicates surfaces a planted near-duplicate pair; read tools write nothing; existing Mnesis tests still pass. `pytest -q` green.

ON DONE: commit ("feat(mnesis): expose maintenance ops over MCP"), report the tool list and which write.
```

---

## Prompt M2 — Maintenance Agent Skills (dream-cycle routines)

```
CONTEXT: Maintenance routines must be declarative, portable Agent Skills (agentskills.io) the agent loads by progressive disclosure - not hardcoded logic. Author the dream-cycle routines as skills over the Mnesis MCP tools.

OBJECTIVE: Create the maintenance SKILL.md skills, discoverable and activatable by the F3 skills subsystem, each encoding its auto-vs-propose policy.

BUILD:
- Under the agent skills directory, one folder per routine with a conformant SKILL.md (name + description + instructions per the agentskills.io spec; concise description for discovery, fuller procedure in the body):
    * decay-sweep: call mnesis_decay; summarize stale/reactivated counts. AUTO-APPLY (safe).
    * graph-hygiene: call mnesis_graph_lint (report); then mnesis_graph_lint(fix=True) for the safe categories; summarize; flag anything needing human attention. AUTO-APPLY safe fixes only.
    * contradiction-triage: list open contradictions (mnesis_review); for each, PROPOSE which page to keep using confidence/recency/source-count with a rationale. DO NOT resolve - emit proposals.
    * deduplication: call mnesis_find_duplicates; for strong candidates PROPOSE a merge/supersession with rationale. DO NOT apply - emit proposals.
    * quality-sweep: call mnesis_health_report; flag orphan pages, no-source pages, low-confidence clusters. Read-only findings.
  Each SKILL.md states explicitly what it MAY auto-apply vs MUST only propose, and the structured output it returns.
- Bundle a small scripts/ helper only where deterministic post-processing genuinely helps; keep the procedure in the instructions.
- Conform to the agentskills.io SKILL.md schema; the F3 loader must discover them by name+description and activate them on demand.

CONSTRAINTS:
- Model/provider-agnostic; reach Mnesis only through the MCP tools.
- The propose-vs-auto boundary is encoded per skill: only hygiene (decay, safe graph fixes) auto-applies; anything changing knowledge meaning is proposal-only.
- Valid, progressive-disclosure-friendly SKILL.md (metadata light, body fuller).

ACCEPTANCE:
- tests: the F3 loader discovers all maintenance skills by name+description (metadata only) and activates each; frontmatter validation passes; running each skill's procedure against fake Mnesis tools (stub) yields the documented structured output and performs NO out-of-policy writes (triage/dedup resolve/apply nothing). `pytest -q` green.

ON DONE: commit ("feat(agents): maintenance agent skills"), report each skill's auto-vs-propose policy.
```

---

## Prompt M3 — The MaintenanceAgent (dream-cycle graph)

```
CONTEXT: Build the concrete MaintenanceAgent on the F4 abstraction: a scheduled dream cycle that loads the maintenance skills (M2) and runs them as passes under F6 governance, driving the Mnesis MCP tools (M1).

OBJECTIVE: Implement the MaintenanceAgent as a LangGraph dream cycle executing a configurable plan of skill-driven passes, auto-applying safe ops and collecting proposals, returning a structured report.

BUILD:
- MaintenanceAgent(profile) on the F4 MaintenanceAgent base: tools = the Mnesis maintenance MCP tools; skills = the M2 maintenance skills; write policy per F6 (auto safe hygiene; propose knowledge-changing ops); budgets from F6.
- A configurable dream-cycle plan: an ordered list of passes (default: quality-sweep -> decay-sweep -> graph-hygiene -> contradiction-triage -> deduplication). Each pass activates its skill, executes it, and collects structured output. Passes are resilient - one failing pass is recorded and the cycle continues.
- A LangGraph graph orchestrating the passes (sequential default), aggregating results. Auto-applied actions go through the governed tools; proposals are accumulated, not applied.
- run_dream_cycle(plan?) -> DreamCycleReport { started, ended, passes:[{name, status, summary, auto_applied, proposals}], health_before, health_after, totals }.

CONSTRAINTS:
- Reaches Mnesis only via MCP tools (F2); imports nothing from the mnesis package.
- Auto-applies only what the skills mark safe; all knowledge-meaning changes are proposals.
- Resilient and idempotent-friendly: a failing pass doesn't abort the cycle.

ACCEPTANCE:
- tests/test_maintenance_agent.py (stub model + fake Mnesis tools + M2 skills): a cycle runs all passes, auto-applies decay + safe graph fixes (observable as tool calls), accumulates contradiction/dedup proposals WITHOUT resolving/applying, and returns a populated report with health_before/after; a deliberately failing pass is recorded and the cycle completes; budget caps are honored. `pytest -q` green.

ON DONE: commit ("feat(agents): maintenance dream-cycle agent"), report the default pass plan and the report shape.
```

---

## Prompt M4 — Proposals, reporting, crystallization, schedule

```
CONTEXT: A dream cycle should surface its proposals for human review, record what it did, optionally remember itself, and run on a cadence.

OBJECTIVE: Route proposals to the review surface, persist/expose the report, crystallize a maintenance digest, and schedule the cycle.

BUILD:
- Proposals: route contradiction-resolution proposals to the existing contradiction review queue (annotating, not resolving); record merge/dedup proposals via a proposals store (a generic queue) so the Web UI review screen (G11) - or a later screen - can show them. Nothing auto-resolves or auto-merges.
- Reporting: persist each DreamCycleReport to the F6 audit log plus a human-readable summary; expose the latest via the runner and a CLI `mnesis-agents dream-cycle --report`.
- Crystallization (meta-memory, configurable, default off): file a concise maintenance digest back into Mnesis via mnesis_file_back/mnesis_ingest (a maintenance-kind digest) so Mnesis records its own dream cycles. Bounded and governed (Mnesis redaction still applies).
- Schedule: register the MaintenanceAgent against the F5 ScheduleTrigger (default nightly cron, configurable) plus on-demand `mnesis-agents dream-cycle --now`. Idempotent across repeated runs.

CONSTRAINTS:
- Proposals are never auto-applied - they wait for human approval through the existing review machinery.
- Crystallized digests carry no secrets/PII (Mnesis redaction on ingest still binds).
- Schedule configurable; on-demand always available; repeated runs are safe.

ACCEPTANCE:
- tests (stub): a cycle's proposals land in the review/proposals surface and are NOT applied; the report is persisted and retrievable; with crystallization on, a maintenance digest is filed (visible via fake mnesis_get); the scheduled trigger fires a cycle and `--now` works; repeated runs are idempotent. `pytest -q` green.

ON DONE: commit ("feat(agents): dream-cycle proposals, reporting, crystallization, schedule"), report the proposal surface and the default cadence.
```

---

## Prompt M5 — Deploy the dream cycle, retire the sidecar, finalize

```
CONTEXT: Run the dream cycle as part of the deployed agent runtime, and retire the deployment-level maintenance sidecar (D5) it replaces.

OBJECTIVE: Register the MaintenanceAgent in the runtime, run it on schedule in Compose, retire the D5 sidecar, and finalize docs + verification.

BUILD:
- Register the MaintenanceAgent in the F5 runner so `docker compose --profile agents up` runs the scheduled dream cycle against Mnesis over MCP. Under the local-llm profile it uses the local model (on-prem).
- Retire the D5 maintenance sidecar (the cron container running mnesis decay / graph-lint): remove or disable it, ensuring EXACTLY ONE scheduler now drives periodic maintenance (no double-run). Note the supersession in the deployment docs.
- Docs: README "Maintenance agents (dream cycle)" - the passes, the auto-vs-propose policy, the cadence, the on-demand run, and where proposals surface. CLAUDE.md: the first concrete agent family now curates Mnesis on a schedule, MCP-only and governed.
- Verification: an end-to-end stub test plus a real-stack smoke (a shortened cadence) proving a cycle runs, auto-fixes safe items, queues proposals, reports, and (optionally) crystallizes a digest.

CONSTRAINTS:
- Exactly one scheduler owns periodic maintenance - no D5 + agent double-run.
- The agent reaches Mnesis only over MCP; local-llm makes no external calls.
- No regression to Mnesis or the foundation; existing suites stay green.

ACCEPTANCE:
- `docker compose --profile agents up -d` runs the runtime with the maintenance agent scheduled; a shortened-cadence cycle executes, auto-applies decay + safe graph fixes, queues contradiction/dedup proposals for review, and writes a report; the D5 sidecar is gone and maintenance is not double-run; `--profile local-llm` keeps it on-prem; full suite green.

ON DONE: commit ("feat(agents): deploy dream-cycle, retire maintenance sidecar, docs"), report the run recipe and confirm the sidecar is retired.
```

---

## Verifying the maintenance agents (after M5)

1. `pytest -q` across Mnesis and the agent package — green, fully offline.
2. **MCP surface:** `mnesis_graph_lint`, `mnesis_health_report`, `mnesis_find_duplicates` are callable over the MCP endpoint; the read tools write nothing.
3. **Skills:** the five maintenance skills are discovered by name+description and activate on demand — progressive disclosure holds.
4. **Dream cycle:** `mnesis-agents dream-cycle --now` runs all passes, auto-applies decay + safe graph fixes, and returns a report with `health_before`/`health_after`.
5. **Propose, don't apply:** contradiction and dedup proposals appear in the review surface and are **not** resolved/merged; a human approving one (via the existing review flow) is what actually changes knowledge.
6. **Self-record:** with crystallization on, a maintenance digest is filed and findable in Mnesis.
7. **Scheduled & singular:** `docker compose --profile agents up -d` runs the cycle on cadence; the D5 sidecar is gone and maintenance runs exactly once per tick; `--profile local-llm` keeps it on-prem.

If all seven hold, Mnesis curates itself on a schedule — safe hygiene applied automatically, meaning-changing decisions queued for a human — and the foundation is proven by a real, governed, skills-driven agent family.

---

## Notes for running with Claude Code

- Run M1 → M5 in order on Opus 4.8, after the foundation (F1–F7). M1 is a Mnesis-side change (new MCP tools); M2–M5 are on the agent side and reach Mnesis only through those tools.
- The safety judgement that matters: **the dream cycle auto-applies only reversible hygiene (decay, safe graph fixes) and proposes everything that changes knowledge meaning.** If a diff makes the agent auto-resolve contradictions or auto-merge pages, that is the bug — those are human decisions, routed through the review queue.
- Keep the routines in the **skills**, not hardcoded in the agent — that's what makes the dream cycle extensible and keeps you conformant to the Agent Skills standard. Adding a future pass should mean adding a SKILL.md, not editing the agent.
- Ensure **one scheduler** owns maintenance after M5 — leaving the D5 sidecar running alongside the agent would double-run decay/lint.
- `find_duplicates` is heuristic until Phase-5 vectors; the deduplication skill should treat its candidates as suggestions to propose, never as certainties to merge.
```
