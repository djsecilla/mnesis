# CLAUDE.md — mnesis (MVP PoC)

**This is the schema document: the most important file in the system.** It is the operating contract that turns a general-purpose LLM into a disciplined knowledge worker for this repository. It is read by the coding agent (Claude Code) as project context, and its rules govern how the runtime ingestion LLM writes and curates knowledge.

**Prime rule: keep this file in sync with the code.** If a change to the code alters a field name, a convention, a directory, or a behaviour described here, update this file in the *same* commit. When this file and the code disagree, this file is the intended design and the code is the bug.

---

## 1. What this project is

mnesis is a knowledge base that **compounds** instead of resetting. Retrieval-augmented generation fetches context and forgets it; this wiki accumulates and reinforces it. This repository is the **Phase-1 MVP proof of concept**, implementing the full end-to-end loop:

> **filter → ingest → write a canonical page → index → query → file an answer back → query again and see it surface.**

That last step — a synthesized answer becoming durable, retrievable knowledge — is the compounding behaviour the PoC exists to demonstrate.

---

## 2. Prime directives (invariants)

These hold across every module and every change. Treat a violation as a defect.

1. **Markdown is canonical; the search index is a rebuildable cache.** The Markdown pages under `wiki/pages/` are the single source of truth. The SQLite search index is a projection of them and must be fully reconstructable from Markdown alone. Never persist anything *in the search index* that is not derivable from a page. (The **durable state store** — access events + review queue — is the deliberate exception: it holds state that is *not* derivable from Markdown and is never cleared by rebuild. See §8, "Search index vs state store".)
2. **Filter sensitive data before any write.** Secrets and PII are redacted at the ingestion boundary, *before* a source is persisted or sent to the LLM. A raw secret value must never reach disk, logs, an LLM prompt, or a findings report.
3. **Confidence is derived, never hand-set.** It is computed (Phase 2, `confidence.py`) from Markdown inputs (`source_count`, `last_confirmed`, `contradicts`, decay class) plus an optional access boost from the durable state store — never written into frontmatter. `status` is changed only through the store (so every transition is committed): by `supersede()` and by the decay/lifecycle job (`lifecycle.recompute_all` / `mnesis decay`).
4. **The git history is the audit trail.** Every page mutation is a commit. Do not batch unrelated writes into one commit, and do not rewrite history.
5. **Keep this file in sync** (see Prime rule above).

---

## 3. Repository conventions

Layout (src-layout; everything importable as `mnesis.*`):

```
README.md
CLAUDE.md                 # this file
pyproject.toml            # deps + the `mnesis` console script
.gitignore
.mcp.json                 # MCP registration for Claude Code
src/mnesis/
  config.py               # paths + env config (model, threshold, stub flag)
  store.py                # canonical Markdown + frontmatter + git
  filters.py              # secret / PII redaction (pure functions)
  llm.py                  # Anthropic client wrapper, with offline stub
  ingest.py               # pipeline: filter -> persist source -> extract -> write
  search.py               # SQLite FTS5 index: rebuild / upsert / search
  mcp_server.py           # FastMCP server exposing the wiki tools
  cli.py                  # `mnesis` command
wiki/
  pages/                  # canonical Markdown pages (tracked)
  sources/                # redacted raw sources, for provenance (tracked)
  .index/                 # SQLite index — GITIGNORED (rebuildable cache)
tests/
scripts/demo_end_to_end.py
```

**Environment variables** (read in `config.py`, all with fallbacks):

| Variable | Default | Purpose |
|---|---|---|
| `MNESIS_ROOT` | `./wiki` | Root of pages, sources, and index. |
| `MNESIS_LLM_MODEL` | `claude-sonnet-4-6` | Model used by the ingestion/extraction LLM. |
| `MNESIS_FILEBACK_THRESHOLD` | `0.7` | Quality gate for filing answers back. |
| `MNESIS_LLM_STUB` | unset | When `1` (or no API key), the LLM client returns deterministic canned output so tests and the demo run offline. |

`wiki/.index/` is never tracked by git — it is a cache that `mnesis rebuild` regenerates from the pages.

---

## 4. The page frontmatter schema

Every page is a Markdown file at `wiki/pages/<id>.md` with YAML frontmatter followed by a human-readable Markdown body. **These field names are authoritative** — the `store.Page` model, `ingest.py`, and `search.py` must match them exactly.

| Field | Type | Required | Default | Meaning |
|---|---|---|---|---|
| `id` | string (slug) | yes | — | Stable identifier; collision-safe slug of the title. |
| `title` | string | yes | — | One-line, declarative statement of what the page asserts. |
| `created` | ISO 8601 | yes | now | First write time; never changes. |
| `updated` | ISO 8601 | yes | now | Refreshed on every write. |
| `sources` | list[string] | yes | — | Source references (ids of files in `wiki/sources/`, or URLs). |
| `source_count` | int | yes | `1` | Number of distinct sources supporting the page. *(Confidence input — recorded, not yet scored.)* |
| `last_confirmed` | ISO 8601 | yes | now | When the claim was last confirmed by a source. *(Confidence input.)* |
| `tags` | list[string] | yes | `[]` | `type:value` tags (see §6) plus free tags. |
| `kind` | enum | yes | `fact` | `fact` \| `digest` \| `note` (see §5). |
| `status` | enum | yes | `active` | `active` \| `stale`. Stale pages are deprioritised, not deleted. |
| `supersedes` | string \| null | no | `null` | Id of the page this one replaces. |
| `superseded_by` | string \| null | no | `null` | Id of the page that replaced this one. |
| `contradicts` | list[string] | yes | `[]` | Ids of pages this page directly conflicts with. *(Phase 2; flag-don't-resolve — see §11.)* |
| `decay_class` | string \| null | no | `null` | Optional override of the decay class otherwise inferred from `kind`/`tags`. *(Confidence input — recorded now, scored in Phase 2.)* |
| `relations` | list[{s,p,o}] | yes | `[]` | Typed graph edges (Phase 3): `s`/`o` are `type:value` entity refs, `p` an allowed predicate (§6). The page is the edge's provenance; edge confidence is derived in the graph cache, never here. |
| `question` | string | digest only | — | For `digest` pages: the question that produced the filed answer. |

The **body** is clean Markdown prose. It carries no index or search metadata — those live only in the (rebuildable) index.

Timestamps (`created`, `updated`, `last_confirmed`) are written in UTC ISO 8601 with **microsecond precision** and a `Z` suffix (e.g. `2026-06-10T17:25:20.118087Z`); the appendix examples elide the fraction for readability. `question` is emitted **only** for `digest` pages — `fact`/`note` frontmatter omits it. This is exactly the implemented `store.Page` model.

**Status semantics.** `active` is the normal state — the page participates fully in search. `stale` means the claim is deprioritised: stale pages are **demoted** in search ranking (and excluded from results unless explicitly requested), but they are **never deleted** — reversibility is preferred over destruction (§12). A page goes stale via the lifecycle (low confidence after decay, or being superseded/contradicted); it can return to `active` on reinforcement. *(The transitions are scored in Phase 2; Phase 1 only records the inputs.)*

---

## 5. Page kinds

- **`fact`** — A discrete, sourced claim ingested from external material. The default output of the ingestion pipeline.
- **`digest`** — A synthesized answer filed back via `wiki_file_back` (crystallisation-lite): a question, the answer, and the facts it drew on. This is how exploration becomes durable knowledge. Always tagged so it is distinguishable from ingested facts.
- **`note`** — A human- or agent-authored observation that is neither a single sourced fact nor a synthesized answer. Used sparingly.

---

## 6. Domain vocabulary & graph contract (entities, predicates, relations)

The typed knowledge graph (Phase 3) is built from two things already in Markdown: the **entities** are the `type:value` tags, and the **relations** are structured edges in the `relations` frontmatter field. There is no separate entity field — an entity is just a `type:value` ref.

**Entity ref format:** `type:value`, **lowercase, hyphenated** value (`project:atlas`, `library:redis`, `person:sarah`, `decision:auth-migration`). Normalised by `vocab.normalize_ref`.

**Entity types** (`vocab.ENTITY_TYPES`): `person`, `project`, `library`, `concept`, `file`, `decision`.

**Predicates** (`vocab.PREDICATES`) — directed edge types, written `A -> B`:

| Predicate | Direction semantics |
|---|---|
| `uses` | A makes use of B |
| `depends_on` | A requires B; changing B affects A |
| `owns` | person/team A is responsible for B |
| `caused` | A brought about B |
| `fixed` | A resolved B |
| `contradicts` | A conflicts with B (stored directed, treated symmetric in traversal) |
| `supersedes` | A replaces B |

**Relation = a triple** `{s, p, o}` in a page's `relations` frontmatter list, where `s`/`o` are entity refs and `p` a predicate. **The page that carries the triple is its provenance.** Validated/normalised by `vocab.validate_relation`.

**Edge provenance & confidence are DERIVED** — they live only in the graph cache, never in frontmatter. For each distinct triple: `source_pages` (the asserting page ids), `assertion_count`, and `confidence` = noisy-OR over those pages' Phase-2 confidence, `1 - Π(1 - conf_i)`. Edges supported only by stale/superseded pages are demoted and excluded by default.

Use lowercase, hyphenated values. Prefer an existing ref over a near-duplicate (`library:redis`, never also `lib:Redis`). *(Edge building, traversal, and `impact()` are later Phase-3 steps; this step records and validates relations only.)*

---

## 7. Ingest rules

The pipeline contract (`ingest.py`), in order:

1. **Scrub first.** Run `filters.scrub` on the raw text. Proceed with the redacted text only.
2. **Persist the source.** Save the redacted source via `store.write_source`, which writes `wiki/sources/<source_ref>.md` and commits it as `mnesis: source <source_ref>` for provenance.
3. **Extract.** Call the LLM with the disciplined extraction prompt to produce JSON `{title, summary_markdown, key_facts, tags, relations}` (Phase 3 adds `relations`). Parse robustly: strip code fences; on failure, retry once with a stricter instruction; then fall back to a minimal page built directly from the source. **Normalize & validate** every entity ref (`vocab.normalize_ref`) and every triple (`vocab.validate_relation`); invalid/unsupported triples are **dropped and logged** (never written), and relations are deduplicated. Each entity an edge touches is also recorded as a tag.
4. **Classify & route (Phase 2).** Build the candidate `fact` page, find top-`CANDIDATE_TOP_N` active existing pages via `search.search` (matched on `title`), and classify the new info against each (conservative LLM classifier, defaults to `unrelated`). Route to the lifecycle action below. On **reinforce**, new valid relations and entity tags are unioned (deduped) into the existing page. Ingest upserts every page it touches into the search cache.

**Extraction discipline** (these go in the extraction system prompt):
- Cite the source. State only what the source supports.
- Do not invent facts, names, numbers, or relationships. Mark uncertainty explicitly in the body.
- Write a declarative `title` that states the claim (e.g. "Project Atlas uses Redis for caching"), not a topic label.
- Prefer one coherent claim per page. Group tightly related facts; split unrelated ones.

**Lifecycle routing (Phase 2)** — the classifier label decides the action:
- **`reinforces`** → no new page: append the source, `source_count += 1`, reset `last_confirmed` to now (the retention clock), commit `mnesis: reinforce <id>`.
- **`supersedes`** → write the new page and `store.supersede(old_id, new_page)`: the old page goes `stale` with both links set.
- **`contradicts`** → compute both pages' confidence; if the winner leads the loser by ≥ `AUTO_RESOLVE_MARGIN`, auto-supersede the loser; otherwise both pages coexist, each other's id is added to their `contradicts` list, and `state.enqueue_contradiction` files a review.
- **`unrelated`** → create a new `fact` page (`source_count: 1`, `last_confirmed: now`, `sources: [source_ref]`), the Phase-1 behaviour.

A contradicted page is **never silently deleted** — it is superseded (→ `stale`) or queued for review. Reinforcement resets the retention clock; a mere read (access) does not.

---

## 8. Retrieval contract

- Matching is **BM25 keyword** over `(id, title, tags, body)` via SQLite FTS5, **blended with confidence** (Phase 2): `final_score = bm25_norm * (0.5 + 0.5 * confidence)`. Vector similarity, graph traversal, and reciprocal rank fusion remain out of scope (Phase 5 / Phase 3).
- `search(query, limit=10, include_stale=False)` returns hits with `id`, `title`, `snippet`, `bm25_score`, `confidence`, `final_score`, and `status`, ordered by `final_score`. **Stale pages are excluded unless `include_stale=True`**, and (capped at `STALE_CAP`) never outrank an active page of comparable match.
- Each indexed row **caches** the page's `confidence` (and `computed_at`) in UNINDEXED columns — derived state that lives in the index, never in Markdown. The Markdown-derived part is reproducible by `rebuild()`; the access-boost part comes from the durable state store. So after deleting `wiki/.index/`, `rebuild()` reproduces the deterministic parts (bm25, snippets) and the ranking *order*; exact confidence floats may differ only by the wall-clock delta in retention. A test asserts this.
- **Reads reinforce.** `wiki_get`, and the top hits of `wiki_query`, call `state.record_access` and refresh that page's cached confidence — cheaply and never blocking or failing the query.
- **Graph-augmented (Phase 3).** When a query resolves to an entity, `graph.graph_query` folds in graph-reachable pages (depth-bounded) alongside the keyword hits, adding a small additive `graph_proximity` term that decays per hop (`final += graph_proximity`). A graph-reached page is always presented with its **grounding** (the connecting edge + asserting page) — augment, don't obscure. A query that resolves to no entity is unchanged. `graph.impact(entity, depth=3)` is the headline query: reverse-traverse `depends_on`/`uses` to find what a change to the entity would affect, with paths and grounding pages (demoted edges excluded). All graph access goes through the `GraphBackend` primitives — no engine calls leak into the query path. This is **not** RRF; vectors/RRF remain Phase 5.

### Search index vs state store (refined invariant)

Phase 2 introduces a second store under `wiki/.index/`. The two have different durability, and the distinction is load-bearing:

- **Markdown pages** (`wiki/pages/`) — the single source of truth. Everything else is reconstructable from them.
- **Search index** (`wiki/.index/wiki.db`) — a **rebuildable cache**. A pure projection of the Markdown; `rebuild()` drops and regenerates it and must reproduce identical results. Stores nothing not derivable from a page.
- **State store** (`wiki/.index/state.db`) — **durable, auxiliary state** that is *not* derivable from Markdown: access events (how often/recently a page was read) and the contradiction review queue. It is created on demand and **`search.rebuild()` must never clear or touch it.** Losing it is survivable but lossy.
- **Graph cache** (`wiki/.index/`, Phase 3) — a **rebuildable cache** too: the typed knowledge graph is regenerated from the pages' `relations` (and `type:value` tags). Like the search index it holds nothing not derivable from Markdown + the durable state store (edge confidence is computed from page confidence). `mnesis rebuild` rebuilds **both** the search index and the graph; neither rebuild clears the state store. The graph engine sits behind a pluggable `GraphBackend` interface whose default is an **embedded SQLite** backend (edges table + recursive-CTE traversal); because the graph is a cache, not a system of record, swapping engines (e.g. Postgres+AGE, Neo4j) touches nothing canonical.

Refined rule: **confidence degrades gracefully to its Markdown-only value if the state store is lost.** Confidence is computed from canonical inputs (`source_count`, `last_confirmed`, `contradicts`, decay class) *enriched* by durable state (access recency/frequency). With the state store gone, confidence still computes from Markdown alone — just without the access-based enrichment. So `state.db` is in `wiki/.index/` (gitignored, regenerable location) for convenience, but it is **conceptually separate from the cache**: deleting the *search index* is routine; deleting the *state store* loses access history and open reviews, which cannot be rebuilt from Markdown.

### Confidence model

Confidence is a value in `[0, 1]`, **computed, never hand-set** (`confidence.py`). The formula is illustrative — its constants live in `config.py` and are env-overridable, so tune freely but keep the shape:

```
support   = 1 - 0.5 ** source_count                      # 1 src .50, 2 .75, 3 .875 (saturating)
retention = exp(-days_since(last_confirmed) / S)         # Ebbinghaus decay, S = stability per decay_class
contradiction_factor = 0.6 ** unresolved_contradictions  # len(page.contradicts)
access_boost = min(0.10, 0.02 * recent_access_count)     # from the state store; 0 if state lost

raw  = (W_SUPPORT * support + W_RETENTION * retention) / (W_SUPPORT + W_RETENTION)   # weights default 1, 1
conf = clamp(raw * contradiction_factor + access_boost, 0, 1)
if status == "stale":  conf = min(conf, STALE_CAP)       # STALE_CAP default 0.40
```

**Stability `S` (days) by decay class:** `decision`/`architecture` = 365 · `fact` = 180 · `note` = 60 · `transient`/`bug` = 21. The class is resolved by `resolve_decay_class`: an explicit `decay_class` override wins, else a `decision:`/`architecture:` tag (slow) or `bug:`/`transient:` tag (fast), else the page's `kind` (`digest` falls back to `fact`).

**Two clocks, kept separate:** *retention* anchors on `last_confirmed` (a new confirming source resets it to now); *staleness inactivity* anchors on the most recent access **or** reinforcement. Confidence is derived state — it lives in the index/state layers, **never** in Markdown frontmatter.

**Decay & lifecycle (`lifecycle.recompute_all`, `mnesis decay`).** A periodic pass recomputes every page's confidence (refreshing the cached value) and transitions status, always through the store so each change is one commit (`mnesis: <id> -> stale|active`):
- **active → stale** when *intrinsic* confidence (the stale cap is ignored for this decision) is below `STALE_THRESHOLD` **and** inactivity — `now` minus the most recent of `last_confirmed` or `last_accessed` — exceeds the decay class's `INACTIVITY_DAYS` window. A recently read or freshly confirmed page therefore stays active.
- **stale → active** only on recent **reinforcement** (`last_confirmed` within the window), never on a read alone, and never for a page explicitly `superseded_by` another. Access boosts confidence but cannot by itself revive a stale page.

The pass is **idempotent**: with no time change it makes no transitions and no commits. The scheduler that fires it automatically is Phase 4; for now it is the `mnesis decay` command (MCP `wiki_decay`).

---

## 9. File-back / crystallisation rule

`wiki_file_back(question, answer, quality_score=None)` is the compounding mechanism:

- If `quality_score` (or a simple internal heuristic when `None`) **≥ `MNESIS_FILEBACK_THRESHOLD`**, write a `digest` page that records the `question`, the answer (body), and the facts it drew on (`sources`/`tags`). Return its id.
- Otherwise, **do not file**; return the reason ("below threshold").
- Digest pages are tagged `kind:digest` (and may carry `concept:` tags) so they never masquerade as primary sourced facts.

---

## 10. Quality standards for pages

A page is acceptable when it: states a clear, declarative claim in the `title`; is supported by at least one entry in `sources`; carries consistent `type:value` tags; contains no redaction leak; and does not duplicate an existing page's claim verbatim. Pages that fail are flagged rather than silently kept. (Automated LLM-as-judge scoring at scale is Phase 5; the PoC's bar is the checks above.)

---

## 11. Contradiction handling

**Phase 2 — confidence-margin auto-resolution.** When ingest classifies new info as `contradicts` an existing page, both pages' confidence is compared. If the winner leads by ≥ `AUTO_RESOLVE_MARGIN` (default 0.25), the loser is auto-superseded (→ `stale`, links set). Otherwise — no clear winner — both pages coexist as `active`, each records the other's id in its `contradicts` list (which feeds the `contradiction_factor` penalty, sinking both in search), and `state.enqueue_contradiction` files a review-queue entry.

**Resolving the queue.** `mnesis review` (MCP `wiki_review`) lists open contradictions with each page's current confidence; `mnesis resolve <review_id> --keep <page_id>` (MCP `wiki_resolve`) keeps one page and supersedes the other through `store.supersede` — which clears the mutual `contradicts` link, lifting the kept page's `contradiction_factor` back to 1.0 — then calls `state.resolve_review`. Resolution is always via supersede/status change (no ad hoc edits); the loser stays as `stale` history, never deleted; a resolved review never reappears. `wiki_query`/`wiki_get` flag a returned page that has an open contradiction. LLM-as-judge adjudication remains Phase 5.

---

## 12. Privacy & governance

- **Filter on ingest** is mandatory and automatic (§2.2). The redaction must never leak the original value, including in the findings report.
- **Audit** is the git history plus the saved (redacted) sources. Every write is one commit.
- **Reversibility:** prefer `status: stale` over deletion. The PoC does not hard-delete pages.
- The MVP filter (regex + entropy) is intentionally simple; `detect-secrets` and Microsoft Presidio are the production upgrade path.

---

## 13. Scope: in vs. deferred

**In scope (Phase 1 — implemented):** filtered ingest · Markdown + git canonical store · FTS5 keyword search (rebuildable) · MCP interface with `wiki_ingest` / `wiki_query` / `wiki_file_back` (+ `wiki_get`, `wiki_list`, `wiki_rebuild`) · `mnesis` CLI · end-to-end demo and test. All present and exercised by the test suite.

**In scope (Phase 2 — implemented):** confidence scoring (`confidence.py`) & Ebbinghaus-style decay with the active↔stale lifecycle (`lifecycle.py`, `mnesis decay`); relation-aware ingest — reinforce / supersede / contradict / create (`ingest.py`); the durable **state store** (`state.py`: access events + review queue); confidence-blended retrieval with access-on-read reinforcement (`search.py`); and the contradiction review queue (`mnesis review` / `resolve`). The `contradicts` and `decay_class` frontmatter fields back this. All exercised by the test suite and the `scripts/demo_phase2.py` regression demo.

**In scope (Phase 3 — implemented):** the typed-relationship knowledge graph. The `relations` frontmatter field and entity/predicate vocabulary with validation (`vocab.py`, §6); relation extraction on ingest (`ingest.py`); the pluggable `GraphBackend` (`graph.py`) with an embedded-SQLite default, the rebuild-from-Markdown projection (noisy-OR edge confidence, demotion of stale-only edges), and cycle-safe traversal/neighbors — built into `mnesis rebuild` (search index + graph rebuild together; state store untouched); graph-augmented query and `impact()` wired into retrieval (`wiki_query` folds in graph-reachable pages); the graph tools — `wiki_entity` / `wiki_neighbors` / `wiki_traverse` / `wiki_impact` / `wiki_graph_stats` (MCP) and `mnesis entity` / `neighbors` / `impact` / `graph-stats` (CLI), with query/get noting related entities; and **graph lint** (`graph_lint.py`, `mnesis graph-lint [--fix]`) — auto-fixes the safe categories (merge duplicate edges, demote stale-only edges, recompute confidence) and flags the rest (undeclared/orphan entities, dangling structural edges), idempotently.

**Out of scope for now — map of where each deferred capability lands:**

| Capability | Phase |
|---|---|
| Automation hooks (on-source/session/query/schedule) & scheduler | 4 |
| Vector stream + reciprocal rank fusion; LLM-as-judge quality scoring | 5 |
| Multi-agent mesh sync; private/shared scoping | 6 |

When you extend the PoC toward any of these, **update this file first**, then make the code follow it.

---

## 14. Changing this file

This document is co-evolved with the system. Expect the first version to be rough and to sharpen after the first few dozen sources and lint passes. Conventions:

- Any code change that touches a field, directory, env var, tool, or behaviour described here updates this file in the same commit.
- Add new conventions under the section they belong to; don't scatter them.
- Keep it scannable — it is read at the start of every agent session.

---

## Appendix — Example `fact` page

```markdown
---
id: project-atlas-redis-cache
title: Project Atlas uses Redis for caching
created: 2026-06-09T10:15:00Z
updated: 2026-06-09T10:15:00Z
sources:
  - atlas-architecture-notes
source_count: 1
last_confirmed: 2026-06-09T10:15:00Z
tags: [project:atlas, library:redis, concept:caching, person:sarah]
kind: fact
status: active
supersedes: null
superseded_by: null
contradicts: []
decay_class: null
relations:
  - {s: project:atlas, p: uses, o: library:redis}
  - {s: person:sarah, p: owns, o: decision:auth-migration}
---
Project Atlas uses Redis as its primary caching layer. The auth-migration work
stream depends on this cache. Sarah owns the auth migration.

Source: atlas-architecture-notes. No contradicting sources at time of writing.
```

## Appendix — Example `digest` page (filed back)

```markdown
---
id: what-depends-on-redis-in-atlas
title: What depends on Redis in Project Atlas
created: 2026-06-09T11:02:00Z
updated: 2026-06-09T11:02:00Z
sources:
  - project-atlas-redis-cache
  - atlas-auth-migration-notes
source_count: 2
last_confirmed: 2026-06-09T11:02:00Z
tags: [project:atlas, library:redis, concept:caching, kind:digest]
kind: digest
status: active
supersedes: null
superseded_by: null
contradicts: []
decay_class: null
relations:
  - {s: decision:auth-migration, p: depends_on, o: library:redis}
question: What depends on Redis in Project Atlas?
---
The Redis cache underpins Atlas's caching layer, and the auth-migration work
stream depends on it. Upgrading or replacing Redis therefore puts the auth
migration at risk and should be coordinated with Sarah, who owns it.

Synthesized from: project-atlas-redis-cache, atlas-auth-migration-notes.
```
