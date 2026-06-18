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

Layout (src-layout; the **core** is importable as `mnesis.*`, the **runtime agent** as `mnesis_agent.*`):

```
README.md
CLAUDE.md                 # this file
pyproject.toml            # deps + the `mnesis` and `mnesis-agent` console scripts
.gitignore
.mcp.json                 # MCP registration for Claude Code
src/mnesis/               # the core (canonical store + tools)
  config.py               # paths + env config (model, threshold, stub flag)
  store.py                # canonical Markdown + frontmatter + git
  filters.py              # secret / PII redaction (pure functions)
  llm.py                  # Anthropic client wrapper, with offline stub
  ingest.py               # pipeline: filter -> persist source -> extract -> write
  search.py               # SQLite FTS5 index: rebuild / upsert / search
  mcp_server.py           # FastMCP server exposing the wiki tools
  cli.py                  # `mnesis` command
src/mnesis_agent/         # runtime agent — a separate MCP CLIENT of the core (never imports mnesis.*); see §14
  config.py               # agent env (MCP url/token, LLM, audit dir, local-tools flag)
  mcp_client.py           # MCP HTTP client + ToolSource/ToolSpec abstraction
  fake_tools.py           # FakeToolSource — in-process deterministic stand-in for offline tests
  registry.py             # ToolRegistry: aggregate sources + dispatch
  provider.py             # provider-neutral tool-use (Anthropic / local / stub)
  loop.py                 # bounded reason->act->observe loop + guardrails
  memory.py               # grounding, citations, propose/apply crystallization
  policy.py               # allowlist + write-policy enforcement (hard gate)
  audit.py                # append-only JSONL run audit
  local_tools.py          # opt-in in-process tools (off by default)
  daemon.py               # ingest-daemon directory watcher
  runner.py               # build registry, pick provider, run an archetype
  cli.py                  # `mnesis-agent` command
  profiles/               # the three archetypes (assistant / research / ingest-daemon)
wiki/
  pages/                  # canonical Markdown pages (tracked)
  sources/                # redacted raw sources, for provenance (tracked)
  .index/                 # SQLite index — GITIGNORED (rebuildable cache)
agent_runs/               # agent run audit (JSONL) — GITIGNORED
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
- **`digest`** — A synthesized answer filed back via `mnesis_file_back` (crystallisation-lite): a question, the answer, and the facts it drew on. This is how exploration becomes durable knowledge. Always tagged so it is distinguishable from ingested facts.
- **`note`** — A human- or agent-authored observation that is neither a single sourced fact nor a synthesized answer. Used sparingly.

---

## 6. Domain vocabulary & graph contract (entities, predicates, relations)

The typed knowledge graph (Phase 3) is built from two things already in Markdown: the **entities** are the `type:value` tags, and the **relations** are structured edges in the `relations` frontmatter field. There is no separate entity field — an entity is just a `type:value` ref.

**Entity ref format:** `type:value`, **lowercase, hyphenated** value (`project:atlas`, `library:redis`, `person:sarah`, `decision:auth-migration`). Normalised by `vocab.normalize_ref`.

**Entity types** (`vocab.ENTITY_TYPES`): default `person`, `project`, `library`, `concept`, `file`, `decision`. **Configurable** via **`MNESIS_ENTITY_TYPES`** (comma-separated; empty = default), resolved in `vocab.py` exactly like predicates: custom entries are snake_cased and de-duplicated. There is **no forced core** (unlike predicates), but **`page` is reserved** (`RESERVED_ENTITY_TYPES`) and dropped if supplied — it labels the structural page nodes the graph emits, so an entity type of the same name would collide. The same list-length trade-offs as predicates apply. **UI caveat:** the Web UI assigns distinct colours only to the built-in six types; custom types fall back to the `page` colour unless matching `--entity-<type>` CSS vars are added (a UI rebuild).

**Predicates** (`vocab.PREDICATES`) — directed edge types, written `A -> B`. The set splits into the original engineering relations and a general-purpose set (so non-software knowledge — people, places, history, organisations — forms edges instead of leaving conceptually-related entities as isolated nodes):

| Predicate | Direction semantics |
|---|---|
| `uses` | A makes use of B |
| `depends_on` | A requires B; changing B affects A |
| `owns` | person/team A is responsible for B |
| `caused` | A brought about B |
| `fixed` | A resolved B |
| `contradicts` | A conflicts with B (**symmetric** — see below) |
| `supersedes` | A replaces B |
| `part_of` | A is a component/member of B |
| `located_in` | A is situated in / at B |
| `created` | A brought B into existence (founded / authored / built) |
| `precedes` | A comes before B in time or sequence |
| `influences` | A shapes / affects B (weaker than `caused`) |
| `related_to` | A is associated with B (**symmetric**) — the **last-resort catch-all**; the extraction prompt instructs the model to prefer a more specific predicate first |

`impact()` still reverse-traverses **only `depends_on`/`uses`** (`_IMPACT_PREDICATES`), keeping change-propagation semantics crisp; the new predicates connect and are traversable via `neighbors`/`traverse` but do not widen impact.

**Symmetric (undirected) predicates.** Some predicates have no meaningful direction (`A p B` ⟺ `B p A`). `vocab.SYMMETRIC_PREDICATES` (resolved from **`MNESIS_SYMMETRIC_PREDICATES`**, default `contradicts,related_to`, intersected with the active predicate set; empty disables it) marks them. A symmetric edge is **canonicalised on build** (`vocab.canonical_edge` orders the endpoints) so a reciprocal `A→B` / `B→A` pair **collapses onto one edge** (provenance/`assertion_count`/noisy-OR merged); it is **traversable from either endpoint** (`neighbors` returns it regardless of `direction`, reported as `both`; `traverse` follows it both ways); and the Web UI draws it **without a direction arrow**. Directed predicates are unchanged.

**The predicate set is configurable.** It is resolved in `vocab.py` from **`MNESIS_PREDICATES`** (comma-separated; empty = the default 13 above). Custom entries are normalised to snake_case (`"Part Of"` → `part_of`), and predicate matching at validation is normalised the same way (so `"depends-on"` resolves to `depends_on`). **`CORE_PREDICATES`** (`supersedes`, `contradicts`) are *always* present — the graph emits them as structural page-edges — and `depends_on`/`uses` should be retained in any custom list if `impact()` is used. Changes are **forward-only**: they affect new ingests; existing edges survive a rebuild (which reprojects stored `relations` without re-validating).

**List-length trade-offs** (the schema-design tension): *too few* predicates and real relationships have no valid `p`, so they are dropped and entities strand as isolated nodes; *too many* and near-synonyms (`created`/`founded`/`built`) make the extractor choose inconsistently, so the **same** relationship gets different predicates across pages and edges that should merge (by `assertion_count`/noisy-OR) don't — plus the full list is injected into every extraction prompt, costing tokens (and latency on local models). **Sweet spot: ~8–15 distinct, orthogonal predicates plus one catch-all (`related_to`).** Prefer fewer broad predicates over many narrow synonyms — the vocabulary works best when each relationship maps to exactly one obvious predicate.

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

**Plan / apply split (preview-then-commit).** The pipeline is exposed as two steps so a surface can preview before committing: `plan_ingest(raw_text, source_ref) -> IngestPlan` runs scrub + extract + classify and performs **zero writes and zero commits** (it does not even persist the source — a previewed-then-abandoned source leaves nothing on disk); `apply_ingest(plan, overrides=None) -> IngestResult` honours overrides (edited `title`/`tags`, `rejected_relations`/`accepted_relations`, a forced `routing` `{action, target_page_id}` whose non-`new` target must exist) and performs the writes via the same Phase-2 routing. `IngestPlan`/`overrides`/`IngestResult` are plain serializable dicts (they cross the HTTP boundary for the UI). The one-shot `ingest_source()` is exactly `plan_ingest` then `apply_ingest`, so CLI/MCP behaviour is unchanged.

---

## 8. Retrieval contract

- Matching is **BM25 keyword** over `(id, title, tags, body)` via SQLite FTS5, **blended with confidence** (Phase 2): `final_score = bm25_norm * (0.5 + 0.5 * confidence)`. Vector similarity, graph traversal, and reciprocal rank fusion remain out of scope (Phase 5 / Phase 3).
- `search(query, limit=10, include_stale=False)` returns hits with `id`, `title`, `snippet`, `bm25_score`, `confidence`, `final_score`, and `status`, ordered by `final_score`. **Stale pages are excluded unless `include_stale=True`**, and (capped at `STALE_CAP`) never outrank an active page of comparable match.
- Each indexed row **caches** the page's `confidence` (and `computed_at`) in UNINDEXED columns — derived state that lives in the index, never in Markdown. The Markdown-derived part is reproducible by `rebuild()`; the access-boost part comes from the durable state store. So after deleting `wiki/.index/`, `rebuild()` reproduces the deterministic parts (bm25, snippets) and the ranking *order*; exact confidence floats may differ only by the wall-clock delta in retention. A test asserts this.
- **Reads reinforce.** `mnesis_get`, and the top hits of `mnesis_query`, call `state.record_access` and refresh that page's cached confidence — cheaply and never blocking or failing the query.
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

The pass is **idempotent**: with no time change it makes no transitions and no commits. The scheduler that fires it automatically is Phase 4; for now it is the `mnesis decay` command (MCP `mnesis_decay`).

---

## 9. File-back / crystallisation rule

`mnesis_file_back(question, answer, quality_score=None)` is the compounding mechanism:

- If `quality_score` (or a simple internal heuristic when `None`) **≥ `MNESIS_FILEBACK_THRESHOLD`**, write a `digest` page that records the `question`, the answer (body), and the facts it drew on (`sources`/`tags`). Return its id.
- Otherwise, **do not file**; return the reason ("below threshold").
- Digest pages are tagged `kind:digest` (and may carry `concept:` tags) so they never masquerade as primary sourced facts.

---

## 10. Quality standards for pages

A page is acceptable when it: states a clear, declarative claim in the `title`; is supported by at least one entry in `sources`; carries consistent `type:value` tags; contains no redaction leak; and does not duplicate an existing page's claim verbatim. Pages that fail are flagged rather than silently kept. (Automated LLM-as-judge scoring at scale is Phase 5; the PoC's bar is the checks above.)

---

## 11. Contradiction handling

**Phase 2 — confidence-margin auto-resolution.** When ingest classifies new info as `contradicts` an existing page, both pages' confidence is compared. If the winner leads by ≥ `AUTO_RESOLVE_MARGIN` (default 0.25), the loser is auto-superseded (→ `stale`, links set). Otherwise — no clear winner — both pages coexist as `active`, each records the other's id in its `contradicts` list (which feeds the `contradiction_factor` penalty, sinking both in search), and `state.enqueue_contradiction` files a review-queue entry.

**Resolving the queue.** `mnesis review` (MCP `mnesis_review`) lists open contradictions with each page's current confidence; `mnesis resolve <review_id> --keep <page_id>` (MCP `mnesis_resolve`) keeps one page and supersedes the other through `store.supersede` — which clears the mutual `contradicts` link, lifting the kept page's `contradiction_factor` back to 1.0 — then calls `state.resolve_review`. Resolution is always via supersede/status change (no ad hoc edits); the loser stays as `stale` history, never deleted; a resolved review never reappears. `mnesis_query`/`mnesis_get` flag a returned page that has an open contradiction. LLM-as-judge adjudication remains Phase 5.

---

## 12. Privacy & governance

- **Filter on ingest** is mandatory and automatic (§2.2). The redaction must never leak the original value, including in the findings report.
- **Audit** is the git history plus the saved (redacted) sources. Every write is one commit.
- **Reversibility:** prefer `status: stale` over deletion. The PoC does not hard-delete pages.
- The MVP filter (regex + entropy) is intentionally simple; `detect-secrets` and Microsoft Presidio are the production upgrade path.
- **Three surfaces, one core.** The same `mnesis.*` core is reached three ways: the **`mnesis` CLI** (humans/scripts/maintenance), the **MCP server** (agents — stdio locally, HTTP `/mcp` networked), and the **Web UI** (humans — a browser SPA over the REST+SSE gateway at `/api`, the same internals the MCP tools wrap). All three share the canonical store; none has private state. *(The **runtime agent** in `mnesis_agent` is **not** a surface: it is a separately-deployable **client** that reaches the core only across the MCP boundary and never touches the store directly — see §14.)* The HTTP app serves `/mcp`, `/api/*`, and an open `/health`, with bearer auth (`MNESIS_MCP_TOKEN`) on everything but `/health`. The Web UI is now a full **read + write** surface: reading/search/graph/chat, plus ingestion (`/add`, batch `/add/batch`), provenance (`/sources`), and contradiction resolution (`/review`). Every write routes through the **plan→apply** ingestion core (§7) or the supersession machinery (§11) — so writes are previewed, human-confirmed, and committed to git. **Canonical page editing is intentionally out of scope** for every surface: knowledge changes only by ingesting sources (create / reinforce / supersede / contradict) and resolving contradictions, never by hand-editing a page's body, so the audit trail stays a coherent record of *why* each change happened.
- **Deployment model.** `docker compose up` brings up two services: **`mnesis`** (the HTTP MCP+API service, stdio locally / HTTP for networked use) over a volume at `MNESIS_ROOT` (`/data/mnesis`), and **`mnesis-ui`** (a static nginx container serving the SPA and reverse-proxying `/api` + SSE to `mnesis`, injecting the bearer token server-side so the browser never holds it). The durable, must-back-up layer is the **git history** (pages + sources) plus **`.index/state.db`** (access events + review queue). Everything else under `.index/` (`wiki.db`, `graph.db`) is a **regenerable cache** that `mnesis rebuild` reconstructs from Markdown + `state.db` — so backups exclude `.index/` except `state.db`. **`mnesis-ui` is stateless** (no volume) — all state lives in `mnesis`. Profile-gated services: **`--profile agents`** (the **`mnesis-agents-runtime`** — which now runs the scheduled **dream-cycle MaintenanceAgent**, the single owner of periodic maintenance; see §14b) and **`--profile agent`** (the **`mnesis-agent`** ingest-daemon — see §14 — which reaches `mnesis` only over the internal MCP endpoint and is itself stateless: run audit goes to `mnesis-agent-runs`, knowledge stays in `mnesis`). *(The old `--profile maintenance` upkeep sidecar is **retired** — removed from `docker-compose.yml`; the dream-cycle agent drives the same decay/graph-lint upkeep over MCP, so there is exactly one scheduler and no double-run.)* When a client reaches the HTTP MCP endpoint by a name other than localhost (e.g. the in-network `mnesis:8080`), the server's DNS-rebinding Host allowlist must include it: **`MNESIS_MCP_ALLOWED_HOSTS`** (comma-separated `host:port` / `host:*`; empty = localhost only). Compose defaults it to `mnesis:*,localhost:*,127.0.0.1:*`. See `docs/OPS.md`.

---

## 13. Scope: in vs. deferred

**In scope (Phase 1 — implemented):** filtered ingest · Markdown + git canonical store · FTS5 keyword search (rebuildable) · MCP interface with `mnesis_ingest` / `mnesis_query` / `mnesis_file_back` (+ `mnesis_get`, `mnesis_list`, `mnesis_rebuild`) · `mnesis` CLI · end-to-end demo and test. All present and exercised by the test suite.

**In scope (Phase 2 — implemented):** confidence scoring (`confidence.py`) & Ebbinghaus-style decay with the active↔stale lifecycle (`lifecycle.py`, `mnesis decay`); relation-aware ingest — reinforce / supersede / contradict / create (`ingest.py`); the durable **state store** (`state.py`: access events + review queue); confidence-blended retrieval with access-on-read reinforcement (`search.py`); and the contradiction review queue (`mnesis review` / `resolve`). The `contradicts` and `decay_class` frontmatter fields back this. All exercised by the test suite and the `scripts/demo_phase2.py` regression demo.

**In scope (Phase 3 — implemented):** the typed-relationship knowledge graph. The `relations` frontmatter field and entity/predicate vocabulary with validation (`vocab.py`, §6); relation extraction on ingest (`ingest.py`); the pluggable `GraphBackend` (`graph.py`) with an embedded-SQLite default, the rebuild-from-Markdown projection (noisy-OR edge confidence, demotion of stale-only edges), and cycle-safe traversal/neighbors — built into `mnesis rebuild` (search index + graph rebuild together; state store untouched); graph-augmented query and `impact()` wired into retrieval (`mnesis_query` folds in graph-reachable pages); the graph tools — `mnesis_entity` / `mnesis_neighbors` / `mnesis_traverse` / `mnesis_impact` / `mnesis_graph_stats` (MCP) and `mnesis entity` / `neighbors` / `impact` / `graph-stats` (CLI), with query/get noting related entities; and **graph lint** (`graph_lint.py`, `mnesis graph-lint [--fix]`) — auto-fixes the safe categories (merge duplicate edges, demote stale-only edges, recompute confidence) and flags the rest (undeclared/orphan entities, dangling structural edges), idempotently.

**In scope (maintenance ops over MCP — implemented):** the curation/upkeep operations a maintenance agent drives are all reachable behind the same authenticated MCP endpoint, so the agent stays MCP-only. **Writers** (idempotent, git-audited): `mnesis_graph_lint(fix)` (graph lint — report, or apply the safe auto-fixes) and `mnesis_decay` (decay/lifecycle pass). **Read-only, side-effect-free diagnostics** (`maintenance.py`): `mnesis_health_report()` — counts by status/kind, pages with no sources, low-confidence/stale counts, open-contradiction count, graph size + demoted/orphan/undeclared/dangling counts, and search-index/graph freshness vs Markdown; and `mnesis_find_duplicates(limit=20)` — **heuristic** near-duplicate candidate pairs (title/tag overlap, shared edges, FTS co-retrieval) with a rationale, which **proposes and changes nothing** (a stand-in pending the Phase-5 vectors that will replace it with semantic similarity). The contradiction-queue tools `mnesis_review` / `mnesis_resolve`, `mnesis_rebuild`, and `mnesis_graph_stats` round out the set. CLI parity: `mnesis health` and `mnesis find-duplicates`.

**In scope (agent layer — implemented):** the runtime agent (`mnesis_agent`), a separately-deployable MCP **client** that uses Mnesis as memory — MCP client + tool registry, provider-neutral tool-use, a bounded agent loop, memory grounding/crystallization, three archetypes, and the policy/budget/audit/local-tool safety story. Reaches the core only over MCP; never imports `mnesis`. Fully documented in **§14**.

**Out of scope for now — map of where each deferred capability lands:**

| Capability | Phase |
|---|---|
| Session/query automation hooks + a general config-driven hook framework *(on-source ingestion via the W-series connectors/WritingAgent, and scheduled maintenance via the dream cycle, are delivered in §14b)* | 4 |
| Vector stream + reciprocal rank fusion; LLM-as-judge quality scoring | 5 |
| Multi-agent mesh sync; private/shared scoping | 6 |

When you extend the PoC toward any of these, **update this file first**, then make the code follow it.

---

## 14. The agent layer (`mnesis_agent`)

A **runtime agent** that uses Mnesis as long-term memory. It is a *consumer* of the core, not part of it: a separately-deployable package (`src/mnesis_agent/`, importable as `mnesis_agent.*`) that reaches Mnesis **only through the MCP HTTP endpoint** and **never imports `mnesis`**. That boundary is load-bearing — it keeps the agent independently shippable and, crucially, means the agent **cannot bypass Mnesis's own governance** (§2.2, §11): redaction and contradiction/supersession review run server-side on every write, and the agent merely *calls the tool*.

### Architecture — one core, layered client

The package is built bottom-up; each layer has its own module and test file:

1. **MCP client & tool registry** (`mcp_client.py`, `registry.py`). `MCPToolSource` connects to the Mnesis MCP endpoint (streamable-HTTP, bearer token), lists tools, and normalises them to `ToolSpec{name, description, input_schema}`. A `ToolSource` ABC decouples the registry from MCP; `FakeToolSource` is an in-process deterministic stand-in so the whole stack tests offline. `ToolRegistry` aggregates one or more sources and `dispatch(name, args)` routes to the owning source.
2. **Provider tool-use** (`provider.py`). One neutral interface — `complete_with_tools(system, messages, tools) -> AssistantTurn{text, tool_calls, stop_reason, usage}` — over three adapters: **Anthropic** (tool_use blocks), **local** (Ollama/OpenAI-compatible function calling), and a **stub** (scripted, deterministic, offline). Selected by the same `MNESIS_LLM_*` env as the core. Messages/tool-results use a provider-neutral representation; provider differences stay inside the adapters. `usage` is propagated for budgeting.
3. **The bounded loop** (`loop.py`). `run_agent(profile, input, tools, provider, registry)` runs reason→call tools→observe→repeat→answer. **Guardrails** (all with a safe, flagged stop): `max_iterations`, `max_tool_calls`, input-`token_budget`, wall-clock `deadline`, and a `no_progress` detector (repeated identical `(tool, args)`). **Tool errors never crash the loop** — they return to the model as tool-results so it can recover. Returns `AgentResult{final_text, transcript, tools_used, citations, writes, stop_reason, usage, iterations}`. A redaction-safe audit hook fires per step (args reduced to key names). Pure orchestration: no Mnesis-specific logic.
4. **Memory integration** (`memory.py`). Wraps the loop with Mnesis behaviours: **session-start context load** (a bounded `mnesis_query` on the goal, injected into the system prompt so the agent starts grounded); a **citation convention** (cite `[page-id]`; citations in the result come only from real tool results, never invented); and **crystallization** under a write policy — `propose` (returns a `DigestProposal`, writes nothing), `apply` (may call write tools within its allowlist/budget), or `off`.
5. **Archetypes** (`profiles/`). One core, three profiles — each an `Archetype{system_prompt, tool_allowlist, write_policy, write_allowlist, budgets, entry_mode, allow_local_tools}`:

   | Archetype | Tool allowlist | Write policy | Entry mode |
   |---|---|---|---|
   | **assistant** | read tools (`query`/`get`/`entity`/`impact`/`traverse`) | `propose` (never writes itself) | interactive REPL |
   | **research** | read/graph tools + `file_back` (digests only — no `ingest`, no `resolve`) | `apply`, write allowlist = `{file_back}` | batch |
   | **ingest-daemon** | `ingest` + `query`/`get` (read for dedup) | `apply`, write allowlist = `{ingest}` | daemon |

   The **assistant** proposes a digest and surfaces it for the human to confirm (confirming then calls `mnesis_file_back`). **Research** runs a bounded investigation and crystallizes exactly one digest. The **ingest-daemon** is not an LLM loop but a resilient, idempotent directory watcher (`daemon.py`): each new file maps to a stable `source_ref` and is dispatched once via `mnesis_ingest`; re-seeing it is a no-op; a `contradict` outcome is logged with its review id and **left to Mnesis** (the daemon never forces a resolution); one bad file is logged and skipped without aborting.
6. **Safety: policy, budgets, audit, local tools** (`policy.py`, `audit.py`, `local_tools.py`, assembled in `runner.py`).
   - **Policy enforcement (hard gate).** `PolicyEnforcingRegistry` runs `ToolPolicy.check(name)` before *every* dispatch: an out-of-allowlist call, or a write tool under a non-`apply` policy / outside the write allowlist, raises `PolicyViolation` **before any side effect**. The loop catches it and feeds it back to the model as an error result — refused *and* surfaced. This is the hard counterpart to the soft allowlist filtering (which only hides tools from the model).
   - **Budgets** flow from the archetype through the loop's profile and stop the run deterministically with a flagged `stop_reason`.
   - **Audit** (`audit.py`). An **append-only JSONL** run log (one file per UTC day under `MNESIS_AGENT_AUDIT_DIR`, gitignored): `run_start{profile, input}` → one `step` per loop step → `run_end{stop_reason, iterations, usage, tools_used, writes:[{tool, call_id}], citations}`. **Never logs argument values, result bodies, or any redacted secret/PII** — step records carry only `args_keys` and a status; the loop's redaction-safe hook is the only step source.
   - **Local tools (opt-in, off by default).** `LocalToolSource` is the seam for optional in-process tools (e.g. an example `web_search`), registered **only** when `MNESIS_AGENT_ENABLE_LOCAL_TOOLS` is set — a plain run has only Mnesis tools. Even then, `Archetype.allow_local_tools` gates usage to **research alone**; the policy layer refuses local-tool calls from any other profile.

### Run plumbing & CLI

`runner.build_registry(...)` builds the registry (MCP client to Mnesis + optional local tools); `runner.run_archetype(arch, input, registry, provider, *, audit, local_tool_names)` selects the provider, filters tools to the allowlist, wraps the registry in the policy gate, wires the audit trail, and runs the grounded loop. The `mnesis-agent` CLI exposes the three archetypes: `mnesis-agent assistant` (grounded REPL with human-confirmed file-back), `mnesis-agent research "<goal>"` (cited report + crystallized digest id), and `mnesis-agent ingest-daemon --watch <path>`.

### Agent environment variables (read in `mnesis_agent/config.py`, all with fallbacks)

| Variable | Default | Purpose |
|---|---|---|
| `MNESIS_MCP_URL` | `http://localhost:8080/mcp` | Mnesis MCP endpoint the agent connects to. |
| `MNESIS_MCP_TOKEN` | unset | Bearer token; must match the server's `MNESIS_MCP_TOKEN`. |
| `MNESIS_LLM_PROVIDER` / `MNESIS_LLM_MODEL` / `MNESIS_LLM_BASE_URL` / `MNESIS_LLM_STUB` | — | Provider switch, mirroring the core stack (no import of `mnesis.config`). |
| `MNESIS_AGENT_AUDIT_DIR` | `./agent_runs` | Directory for the append-only JSONL run audit. |
| `MNESIS_AGENT_ENABLE_LOCAL_TOOLS` | unset | When set, registers the example local tools (research-only). Off by default. |

### Deployment

The agent ships as part of the stack. The same `mnesis:latest` image carries both packages (the entrypoint's `agent` command runs `mnesis-agent` and **skips** the wiki/git prep — the agent is stateless, no volume). A **profile-gated** Compose service **`mnesis-agent`** runs the ingest-daemon: `docker compose --profile agent up -d`. It `depends_on` `mnesis` (healthy), reaches it at `MNESIS_MCP_URL=http://mnesis:8080/mcp` on the internal network (no host port), mounts a read-only watch directory (`MNESIS_AGENT_WATCH_DIR` → `/watch`), and writes run audit to the `mnesis-agent-runs` volume. The one-off `make agent-research` / `make agent-assistant` reuse the same service definition via `docker compose run --rm`. The **fully-local** recipe is env-driven (no extra container): set `MNESIS_LLM_PROVIDER=local` in `.env` and both `mnesis` and the agent target the host Ollama — agent + Mnesis + model in one trust boundary, no external inference.

Because the daemon reaches the server by service name, the core's HTTP MCP endpoint must allow that Host (`MNESIS_MCP_ALLOWED_HOSTS`, §12). And so an automated client can report each ingest outcome without forcing a resolution, the **`mnesis_ingest` tool surfaces the routing `action:`** (new / reinforce / supersede / contradict) and, when present, the `superseded:` / `review:` ids — the daemon parses these (it tolerates both that text and a JSON `IngestResult`).

### Mnesis now has agents that both consume and contribute

The compounding loop closes at the agent level: agents **read** memory (grounded, cited retrieval) **and write back to it** (research crystallizes digests; the daemon ingests new sources) — all **only through MCP**, never the internals. Governance is unchanged and unbypassable: every agent write goes through the same server-side redaction and contradiction/supersession review as any other write (§2.2, §7, §11). The agent decides *whether* to call a write (policy); Mnesis decides *how* the write is made safe.

### Invariants specific to the agent layer

1. **MCP-only access.** The agent reaches Mnesis solely through MCP tools; it never imports `mnesis` and holds no canonical state of its own.
2. **Mnesis governance is unbypassable.** Per-write safety (redaction, contradiction/supersession review) is the server's job; the agent only calls the tool. The policy layer governs *whether* the agent may call a write, not *how* the write is made safe.
3. **Every limit has a safe, flagged stop.** Out-of-allowlist and out-of-budget calls are refused deterministically, before any side effect.
4. **The audit never holds secrets, PII, or full payloads** — statuses and ids only.
5. **Optional tools are opt-in.** A plain run starts with only the Mnesis tools, and only the research profile may ever use local ones.

---

## 14b. The LangGraph agentic foundation (`mnesis_agents`)

A second, **LangGraph-based** agentic foundation (`src/mnesis_agents/`, importable as `mnesis_agents.*`) coexists with the A-series `mnesis_agent` layer. It is the substrate concrete agents are built on; the base, the category abstractions, the runtime, and concrete agents — the schedule-triggered dream-cycle **`MaintenanceAgent`** and the event-triggered **`WritingAgent`** (notes inbox → Mnesis), each over its connector/skill pipeline — exist today. Like the A-series, it reaches Mnesis **only over MCP** and **never imports `mnesis`**.

- **Multi-LLM, provider-agnostic.** A shared factory (`src/mnesis_llm/factory.py`) maps a provider key to a LangChain chat model. **Mnesis is now provider-agnostic too**: `mnesis.llm.complete()` keeps its native `stub`/`local`/`anthropic` paths unchanged (no regression) and routes the *broader* providers (`openai`/`google`/`mistral`/`bedrock`/`ollama`/`openai_compatible`) through the same factory — so one `MNESIS_LLM_PROVIDER` switch changes the model for **both** Mnesis and the agents, no code change. langchain is a lazy import; the offline stub needs none of it.
- **Mnesis tools via langchain-mcp-adapters** (`knowledge.py`): the `mnesis_*` tools become LangChain tools; a `ToolRegistry` aggregates sources (namespacing only on collision); a `FakeMnesisTools` source makes the layer testable offline.
- **Agent Skills (agentskills.io)** (`skills/`): SKILL.md folders with strict three-level progressive disclosure (discovery = name+description only; activation loads instructions; resources/scripts on demand, path-confined + bounded). Surfaced to any model via skill cards in the prompt + a `use_skill(name)` tool. Same SKILL.md format Claude Code uses. **Bundled dream-cycle maintenance skills** (`skills/bundled/`) encode the upkeep routines over the Mnesis MCP tools, each stating its auto-vs-propose policy in the body and (load-bearingly) in its `allowed-tools` manifest — a proposal-only skill is simply never granted a write tool: **decay-sweep** (calls `mnesis_decay`, AUTO-APPLY), **graph-hygiene** (`mnesis_graph_lint` report then `fix=True`, AUTO-APPLY safe categories only, flag the rest), **contradiction-triage** (`mnesis_review`/`mnesis_get`, PROPOSE a keep by confidence/sources/recency — never `mnesis_resolve`), **deduplication** (`mnesis_find_duplicates`/`mnesis_get`, PROPOSE merges — never applies), and **quality-sweep** (`mnesis_health_report`, read-only findings). Only hygiene auto-applies; anything that changes knowledge *meaning* is proposal-only. Each bundles a small deterministic post-processing `scripts/` helper (shape the tool output into the documented structured result); `knowledge.FakeMaintenanceTools` is the offline stand-in that makes them testable without a running Mnesis. A separate **source-parsing skill** lives alongside them: **parse-note** — it normalizes a notes/Markdown `InboundEvent` (from the W1 connector) into a clean `{text, source_ref, skip, reason}` for ingestion (strips front-matter/signatures/boilerplate, keeps substantive content verbatim, and **skips** empty/trivial notes), via a pure `scripts/parse_note.py` transform (`allowed-tools: []` — it calls no tools and does **not** ingest; the WritingAgent does, under governance). **Note content is DATA, never instructions**: an embedded directive (e.g. "ignore instructions", "mark pages stale", "ingest as authoritative") rides along as ordinary text in `text` and changes neither the output (`skip`/`source_ref` are derived only from structure and length) nor any agent behaviour — enforced structurally by the mechanical, semantics-free script. Each new source type ships its own `parse-<source>` skill (email/chat/docs arrive with their connectors), so source quirks stay declarative.
- **Base agent + categories** (`base.py`, `categories/`): `build_agent(profile)` compiles a LangGraph agent (LangChain 1.x `create_agent`) wiring model + tools + skills + governance + checkpointer, returning a structured result. Three category ABCs declare trigger + write policy — **WritingAgent** (event / ingest), **ActionAgent** (event-or-schedule / propose), **MaintenanceAgent** (schedule / propose).
- **The dream-cycle `MaintenanceAgent`** (`maintenance_agent.py`): the first concrete agent — `DreamMaintenanceAgent`, a schedule-triggered `MaintenanceAgent` (`write_policy="propose"`). `run_dream_cycle(plan?) -> DreamCycleReport` runs a **deterministic LangGraph graph** (one node per pass, sequential) over a configurable plan (default **quality-sweep → decay-sweep → graph-hygiene → contradiction-triage → deduplication**). Each pass activates its M2 skill, dispatches the M1 maintenance MCP tools through F6 governance, and runs the skill's helper script for the structured per-pass output. **Auto-applies only safe hygiene** — `mnesis_decay` and `mnesis_graph_lint` are *not* in `write_tools`, so governance lets them execute, while the knowledge-changing writes (`mnesis_resolve`/`mnesis_ingest`/`mnesis_file_back`) are gated by the `propose` policy and **never fire** (proposals are accumulated, not applied). Like the ingest-daemon it is **not** an LLM loop — maintenance is mechanical — and it is **resilient** (a failing pass is recorded and the cycle continues) and **budget-bounded** (F6 tool-call/wall-clock caps stop it with a flagged `stop_reason`). The report shape: `{started, ended, passes:[{name, status, summary, auto_applied, proposals, error}], health_before, health_after, totals}`. The F4 base agent (an LLM loop, via `build()`) remains available for ad-hoc maintenance chat.
  - **Proposals, reporting, crystallization, schedule** (`run_and_record()`, `proposals.py`, `reports.py`). After a cycle: (1) **proposals** are routed to a generic, append-with-upsert **proposals queue** (`ProposalStore`, gitignored JSONL) — `contradiction` proposals carry the Mnesis `review_id` so they **annotate** that open review by id (a recommended keep, **never** `mnesis_resolve`), and `duplicate` proposals key on the unordered page pair; nothing is auto-applied, and re-proposing the same thing upserts one entry (idempotent). The Web UI review screen (G11) reads this queue. (2) Each **`DreamCycleReport`** is persisted (`DreamReportStore`, JSONL history + latest human summary) and mirrored into the **F6 audit log** as a `dream_cycle` record (counts/statuses/ids only — never rationale text or values); the latest is exposed via `mnesis-agents dream-cycle --report`. (3) **Crystallization** (meta-memory, `MNESIS_AGENTS_CRYSTALLIZE`, **default off**) files a bounded, governed maintenance digest of the cycle back into Mnesis via `mnesis_file_back` (one-shot `apply` gate, single write tool, length-capped) — Mnesis's server-side redaction still binds. (4) **Schedule** (`register_dream_cycle`, F5): the cycle subscribes to the runner on a cadence — default a **nightly cron `0 3 * * *`** (`MNESIS_AGENTS_DREAM_CRON`; cron needs the APScheduler extra, an interval via `MNESIS_AGENTS_DREAM_INTERVAL_SECONDS` uses the bundled scheduler) — and is always available on-demand via `mnesis-agents dream-cycle --now`; the handler runs off the loop and repeated firings are idempotent.
- **Triggers + runner** (`triggers/`, `registry.py`, `runner.py`): event and schedule triggers; an `AgentRegistry` of subscriptions; a resilient, observable `Runner`. `mnesis-agents run` registers the scheduled dream cycle (above) and otherwise stays a healthy idle host.
- **The SourceConnector pattern + connectors** (`triggers/connector.py`, `connectors/`): **THE contract every inbound source implements** (notes, email, chat, docs). A `SourceConnector` (an `EventTrigger`) *only detects and normalizes* — it **never calls Mnesis or an LLM** (that is the WritingAgent's job downstream). Shape: a **lifecycle** (`start()`/`stop()`; `stream()` yields events, so it is a drop-in trigger for the runner); **idempotency** via a durable `ProcessedStore` keyed by `(source_ref, content_hash)` (re-seeing identical content is a no-op; a new hash for the same ref re-emits; `ack()` marks processed after dispatch); and **error surfacing** — a bad item becomes a `ConnectorError` (in `self.errors` + an optional handler), never a crash, so one bad item never stops the watch. A subclass implements one method, `poll_once()` (a single detection pass that builds and `submit()`s events); the base drives both **poll** (timed re-scans) and **watch** (filesystem events via `watchdog`, in the `agents` extra; falls back to poll if absent) modes from it. Events use the normalized **`InboundEvent` envelope** — `{source_type, source_ref, kind, text, content_hash, metadata}` (built by `InboundEvent.from_source`, which mirrors them onto the generic runner fields `source`/`payload`/`id`). The first concrete connector is **`NotesInboxConnector`** — it watches `MNESIS_NOTES_INBOX` for new/changed `.md`/`.txt` notes and emits one event per note with a **stable** `source_ref` of `note:<relative-path>` and a `content_hash` over the text. Config: `MNESIS_NOTES_INBOX` · `MNESIS_NOTES_MODE` (`poll`|`watch`) · `MNESIS_NOTES_POLL_INTERVAL` · `MNESIS_NOTES_MAX_BYTES` · `MNESIS_NOTES_SUFFIXES`; the connector ledger lives under `MNESIS_AGENTS_CONNECTOR_STATE_DIR` (gitignored).
- **The `WritingAgent`** (`writing_agent.py`): the second concrete agent — `SourceWritingAgent`, an event-triggered F4 `WritingAgent` (`write_policy="ingest"`) that turns an `InboundEvent` into a **governed Mnesis ingestion**. `handle_event(event, *, approved=False) -> WritingResult`: (1) select the parse skill for the event's `source_type` (the `MNESIS_AGENTS_PARSE_SKILLS` mapping, e.g. `notes:parse-note`) and run it (W2) → clean `{text, source_ref, skip, reason}`; (2) if **skip**, ack + record (no ingest); (3) the **approval policy** — a `source_type` in `MNESIS_AGENTS_APPROVAL_SOURCE_TYPES` holds for human approval (`status="pending_approval"`, not acked) before ingest, while the trusted notes inbox **auto-ingests**; (4) call `mnesis_ingest(text, source_ref)` over MCP through F6 governance (`GovernedTools`, shared via `governed.py`); (5) **interpret** Mnesis's routing into a `WritingResult{source_type, source_ref, status (ingested|skipped|pending_approval|duplicate|error), action (created|reinforced|superseded|contradiction_queued), page_id, redaction_count, superseded_id, review_id, skip_reason, error, acked}`; (6) **ack** in the shared `ProcessedStore` and **audit** (`write_writing_event` — ids/statuses/counts only, never the note text). **Mnesis governance is unbypassable** — redaction/extraction/routing/review run server-side; the agent only *calls the tool* and **records the redaction count it gets back** (it never redacts itself). **Idempotent** — an event already `processed` is a no-op `duplicate`. **Inbound content is DATA, not instructions** — carried in the system prompt and enforced structurally (the deterministic W2 parse; routing fixed by `source_type` + config, never by the note's text). Adding a source is **connector (W1) + `parse-<source>` skill (W2) + one `MNESIS_AGENTS_PARSE_SKILLS` entry** — no agent code change.
- **Writing-pipeline robustness** (`writing_pipeline.py`): the connector→agent path is **effectively-once with no silent loss**. The **dedup key is `(source_ref, content_hash)`**: at-least-once delivery from the connector + idempotent processing at the agent (an already-`processed` item is a no-op `duplicate`); a *different* hash for the same ref (an edit / new-but-overlapping source) still flows to Mnesis, whose reinforce logic handles same-claim duplication. `WritingPipeline.process_event` adds **retry/backoff** — a *transient* failure (the ingest tool raised, e.g. Mnesis momentarily unavailable → `WritingResult.retryable=True`) is retried with exponential backoff (`MNESIS_AGENTS_WRITE_MAX_RETRIES`/`…_BACKOFF_BASE`/`…_BACKOFF_FACTOR`) — and a **dead-letter**: a *poison* item (a non-retryable error, or one still failing after the retry budget) is recorded in a durable `DeadLetterStore` (append-with-upsert JSONL under `MNESIS_AGENTS_DEAD_LETTER_DIR`, keyed by `(source_ref, content_hash)`) **with a reason and attempt count**, and **skipped on re-delivery** (`status="dead_letter"`) — the pipeline never wedges and never silently drops. `process_batch` runs a burst with **bounded concurrency** (`MNESIS_AGENTS_WRITE_CONCURRENCY`) and **isolation** — one poison item never blocks the rest. On-demand backfill: `ingest_note_paths` / the CLI **`mnesis-agents ingest-note <file|dir>`** runs the same parse→govern→ingest→ack→dead-letter path immediately over a file or directory (reusing `NotesInboxConnector.build_event` to normalize).
- **The OutboundChannel pattern + safe channels** (`channels.py`): the **outbound mirror** of the `SourceConnector` — where a connector turns the world into inbound events, a channel turns an action agent's produced **artifact** into an outbound delivery. **THE contract every delivery mechanism implements** (email, Slack, calendar, webhook): `deliver(artifact, destination, context) -> DeliveryResult`, a `name`, and a load-bearing **`risk_class`** — `inert` (local/operator-scoped; nothing reaches a third party) or `external` (leaves the box / reaches a third party). A channel **only delivers; it does not decide whether it is allowed to run** — that is the gate's job (A2). The base defaults `risk_class` to **`external`** (the conservative, gated side) so an undeclared channel is treated as risky, and the gate can treat every `external` channel as **always-gated**. **Only INERT channels ship here** — there is deliberately no external-send channel: **`DraftOutboxChannel`** (`risk_class=inert`) writes the artifact as a Markdown draft (YAML metadata header + body) to `MNESIS_ACTION_OUTBOX` and returns the path — never sends; **`LocalNotifyChannel`** (`risk_class=inert`) notifies only the local operator (console/log + a JSONL `MNESIS_ACTION_NOTIFY_FILE`), no third-party recipient. A `ChannelRegistry` maps names → instances (`default_channel_registry()` = the two inert channels) for the action agent to deliver through. Both deliveries **report failure, never raise**.
- **The approval gate** (`action_gate.py`, the **safety keystone**): **no channel executes without a human approval** — the gate is the **single, fail-closed path** to any side effect (a channel is *only ever* invoked from inside `ActionGate._execute`). It is the durable, out-of-band counterpart of the F6 in-loop human-in-the-loop interrupt. `propose(action_type, channel, artifact, destination, rationale) -> ActionProposal` (1) validates **destination integrity** — the destination comes from policy/user input, *never* from Mnesis content or the artifact: an artifact carrying a destination-control field (`to`/`recipient`/`bcc`/… in its metadata) is **refused** (`DestinationIntegrityError`; anti-exfiltration/injection), re-checked on the executed artifact so an edit can't smuggle one; (2) enforces the **always-gated rule** — `_must_gate` returns True for **every `external` channel regardless of policy**, and for `inert` unless the off-by-default `MNESIS_ACTIONS_AUTO_RUN_INERT` escape hatch is set (external can *never* auto-run); (3) records a **pending** `ActionProposal{id, action_type, channel, risk_class, artifact, destination, rationale, status, created, …}` and **pauses**, executing nothing. A human then **approves** (execute the channel **exactly once**), **edits** (execute the edited artifact/destination — still integrity-checked), or **rejects** (discard, deliver nothing); a decided proposal can't be re-run (`GateError`). Proposals persist in an **`ActionProposalStore`** (the M4 proposals store extended for actions, `action_proposals.jsonl`). Every outcome — `proposed`/`executed`/`execute_failed`/`rejected`/`auto_executed` — is **audited** (`write_action_event`: artifact *identity* — kind/title/length — channel, risk, destination, result; **never the artifact body**). Approvals surface: the CLI **`mnesis-agents actions [list|approve|reject] <id>`** (`--destination`/`--title`/`--body-file` edit on approve), designed so the Web review screen (G11) can show them.
- **Governance/persistence/observability** (`governance.py`, `audit.py`): fail-closed allowlist + write-policy + budgets (LangChain middleware), a SQLite (default) / Postgres checkpointer, HumanInTheLoop approval interrupts, an append-only JSONL audit (names/statuses/ids only — never values), and **opt-in** LangSmith tracing (off unless its env is set). Per-write safety stays Mnesis's job server-side.
- **Deployment.** The single `mnesis:latest` image carries both packages (installed with the `agents` extra). A profile-gated Compose service **`mnesis-agents-runtime`** (`docker compose --profile agents up -d`) runs `mnesis-agents run`, which registers **two concrete agents** (each over MCP, each gated by an enable flag, each registered resiliently — if Mnesis is unreachable at startup the runner comes up rather than crashing):
  - the scheduled dream-cycle **`MaintenanceAgent`** (`MNESIS_AGENTS_DREAM_ENABLED`, default on) — the **single owner of periodic maintenance** now that the D5 `--profile maintenance` sidecar is retired. Cadence via `MNESIS_AGENTS_DREAM_INTERVAL_SECONDS` (the bundled scheduler is interval-based; precise cron via `MNESIS_AGENTS_DREAM_CRON` needs the APScheduler extra). On-demand: `mnesis-agents dream-cycle --now` / `--report`, `make dream-now` / `make dream-report`, `scripts/smoke_dream_cycle.sh`.
  - the notes-inbox **`WritingAgent`** (`MNESIS_NOTES_ENABLED`, default on; `register_notes_writer` wires the `NotesInboxConnector` as a runner event-trigger + a `notes-writer` subscription running the W4 pipeline) — watches the bind-mounted inbox (`MNESIS_NOTES_INBOX=/data/notes_inbox`, host `${MNESIS_NOTES_INBOX_DIR:-./notes_inbox}`, read-only; `poll` mode default in containers) and ingests new/changed `.md`/`.txt` notes. On-demand backfill: `mnesis-agents ingest-note <file|dir>` / `make ingest-note NOTE=…`, `scripts/smoke_notes_inbox.sh`. The runner stops stateful triggers (connectors) cleanly on shutdown.

  MCP-only; durable state + run audit + proposals/reports + the connector ledger + the dead-letter on volumes (`MNESIS_AGENTS_CONNECTOR_STATE_DIR=/data/agents_runs/connectors`). `MNESIS_LLM_PROVIDER=local` keeps the whole stack on-prem — neither agent makes model calls (the dream cycle is deterministic; the writing agent only calls `mnesis_ingest`, and Mnesis runs extraction on the local model).

---

## 15. Changing this file

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
