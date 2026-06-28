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
4. **The git history is the audit trail.** Every page mutation is a commit. Do not batch unrelated writes into one commit, and do not rewrite history. Each **tenant** has its own git repo under `tenants/<id>/` (§16).
5. **The store is tenant-scoped by construction.** There is no global/ambient store: every store object (`Store`, `SearchIndex`, `StateStore`, the graph backend) is built from a `TenantContext`, every path is resolved against and guarded within that tenant's root, and the active tenant is resolved at a boundary (`tenancy.current()` fails closed if none is bound). Cross-tenant access is structurally impossible, not merely checked. See §16.
6. **Keep this file in sync** (see Prime rule above).

---

## 3. Repository conventions

Layout (src-layout; the **core** is importable as `mnesis.*`, the **agent layer** as `mnesis_agents.*` — see §14):

```
README.md
CLAUDE.md                 # this file
pyproject.toml            # deps + the `mnesis` and `mnesis-agents` console scripts
.gitignore
.mcp.json                 # MCP registration for Claude Code
src/mnesis/               # the core (canonical store + tools)
  config.py               # the data root + env config (model, threshold, stub flag)
  tenancy.py              # the isolation primitive: Tenant, registry, TenantContext (§16)
  auth.py                 # credentials -> (TenantContext, Principal); fail-closed resolver + system-admin (§16)
  authz.py                # role authorization + within-tenant private/shared visibility (§16)
  admin.py                # tenant lifecycle (provision/suspend/delete) + system-admin boundary + system audit (§16)
  quotas.py               # per-tenant resource quotas, fail-closed at the write boundary (§16)
  store.py                # canonical Markdown + frontmatter + git (Store, tenant-scoped)
  filters.py              # secret / PII redaction (pure functions)
  llm.py                  # Anthropic client wrapper, with offline stub
  ingest.py               # pipeline: filter -> persist source -> extract -> write
  search.py               # SQLite FTS5 index: rebuild / upsert / search (SearchIndex, tenant-scoped)
  state.py                # durable access events + review queue (StateStore, tenant-scoped)
  mcp_server.py           # FastMCP server exposing the wiki tools
  cli.py                  # `mnesis` command
src/mnesis_agents/        # the LangGraph agent layer — a separate MCP CLIENT (never imports mnesis.*); see §14
src/mnesis_llm/           # shared provider-agnostic chat-model factory (core + agents)
wiki/                     # the DATA ROOT (MNESIS_ROOT) — holds the tenants + registry, never a store itself
  registry.json           # the tenant registry (metadata) — GITIGNORED, OUTSIDE any tenant root
  credentials.json        # the credential store (HASHED tokens) — GITIGNORED, OUTSIDE any tenant root (§16)
  tenants/<tenant_id>/    # one tenant's canonical store + its OWN git repo (§16)
    pages/                #   canonical Markdown pages (tracked)
    sources/              #   redacted raw sources, for provenance (tracked)
    .cache/               #   SQLite caches: wiki.db, graph.db, state.db — GITIGNORED (rebuildable)
agent_runs/               # agent run audit (JSONL) — GITIGNORED
tests/
scripts/demo_end_to_end.py
```

**Environment variables** (read in `config.py`, all with fallbacks):

| Variable | Default | Purpose |
|---|---|---|
| `MNESIS_ROOT` | `./wiki` | The **multitenant data root** (`config.DATA_ROOT`): holds `tenants/<id>/` and `registry.json`. It is **not** itself a store — there are no global pages/sources/index paths (§16). |
| `MNESIS_DEFAULT_TENANT` | `default` | The tenant a single-tenant deployment runs as transparently. |
| `MNESIS_AUTH_ENABLED` | unset | When set, the HTTP boundary resolves a per-tenant, per-principal **credential** from the bearer token (tenant taken only from the credential; unresolved → denied, no default fallback). Off = legacy single-token + default tenant. (§16) |
| `MNESIS_AUTH_PEPPER` | unset | Optional server-side secret mixed into the token hash at rest; never logged. (§16) |
| `MNESIS_DEFAULT_VISIBILITY` | `shared` | Global fallback for a new page's visibility (`shared`\|`private`) when a tenant has not set its own default. (§16) |
| `MNESIS_CREDENTIAL` | unset | A credential token the **CLI** resolves to a (tenant, principal) for tenant-scoped ops. With `MNESIS_AUTH_ENABLED` and no credential, the CLI refuses tenant ops (fail closed); the credential's tenant overrides any `--tenant` flag. (§16) |
| `MNESIS_ADMIN_CREDENTIAL` | unset | A **system-admin** token the CLI resolves for tenant **lifecycle** (`mnesis admin provision/list/suspend/delete`); a tenant credential is refused. Bootstrap one with `mnesis admin bootstrap`. (§16) |
| `MNESIS_TENANT_MAX_PAGES` / `…_MAX_BYTES` | `0` | Default per-tenant resource quotas (0 = unlimited); per-tenant override on the Tenant record. Fail-closed at the ingest write boundary. (§16) |
| `MNESIS_LLM_MODEL` | `claude-sonnet-4-6` | Model used by the ingestion/extraction LLM. |
| `MNESIS_FILEBACK_THRESHOLD` | `0.7` | Quality gate for filing answers back. |
| `MNESIS_LLM_STUB` | unset | When `1` (or no API key), the LLM client returns deterministic canned output so tests and the demo run offline. |

Each tenant's `.cache/` is never tracked by git — it is regenerated by `mnesis rebuild` from that tenant's pages (+ its durable `state.db`). **Path references elsewhere in this file written as `wiki/pages/…`, `wiki/sources/…`, or `wiki/.index/…` now resolve per-tenant under `tenants/<tenant_id>/{pages,sources,.cache}` — they are reached only through a `TenantContext` (§16), never a global path.**

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
| `owner_principal` | string \| null | no | `null` | The principal that created the page (T4); `null` = unowned/legacy (treated as shared). Set at ingest from the bound principal; not editable by content. |
| `visibility` | enum | yes | `shared` | `shared` (every principal in the tenant) \| `private` (owner-only). Set at ingest from the tenant default or an explicit override. Filtered in the data/query layer (§16). |
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

**Per-tenant (§16).** The search index, the graph cache, and the state store are **all per-tenant** — three separate DB files under that tenant's own `.cache/` (`wiki.db`, `graph.db`, `state.db`), opened from its `TenantContext`. Nothing is shared across tenants: search/graph/traverse/impact/decay and the review queue for tenant A can never surface B's pages, entities, edges, reviews, or access counts, and `mnesis rebuild` reconstructs **only the bound tenant's** caches from **its** Markdown — never crossing roots. (Paths written below as `wiki/.index/…` denote each tenant's `.cache/…`.)

Phase 2 introduces a second store under each tenant's `.cache/`. The three have different durability, and the distinction is load-bearing:

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
- **Three surfaces, one core.** The same `mnesis.*` core is reached three ways: the **`mnesis` CLI** (humans/scripts/maintenance), the **MCP server** (agents — stdio locally, HTTP `/mcp` networked), and the **Web UI** (humans — a browser SPA over the REST+SSE gateway at `/api`, the same internals the MCP tools wrap). All three share the canonical store; none has private state. *(The **agent layer** (`mnesis_agents`) is **not** a surface: it is a separately-deployable **client** that reaches the core only across the MCP boundary and never touches the store directly — see §14.)* The HTTP app serves `/mcp`, `/api/*`, and an open `/health`, with bearer auth (`MNESIS_MCP_TOKEN`) on everything but `/health`. The Web UI is now a full **read + write** surface: reading/search/graph/chat, plus ingestion (`/add`, batch `/add/batch`), provenance (`/sources`), and contradiction resolution (`/review`). Every write routes through the **plan→apply** ingestion core (§7) or the supersession machinery (§11) — so writes are previewed, human-confirmed, and committed to git. **Canonical page editing is intentionally out of scope** for every surface: knowledge changes only by ingesting sources (create / reinforce / supersede / contradict) and resolving contradictions, never by hand-editing a page's body, so the audit trail stays a coherent record of *why* each change happened.
- **Deployment model.** `docker compose up` brings up two services: **`mnesis`** (the HTTP MCP+API service, stdio locally / HTTP for networked use) over a volume at `MNESIS_ROOT` (`/data/mnesis`), and **`mnesis-ui`** (a static nginx container serving the SPA and reverse-proxying `/api` + SSE to `mnesis`, injecting the bearer token server-side so the browser never holds it). The durable, must-back-up layer is the **git history** (pages + sources) plus **`.index/state.db`** (access events + review queue). Everything else under `.index/` (`wiki.db`, `graph.db`) is a **regenerable cache** that `mnesis rebuild` reconstructs from Markdown + `state.db` — so backups exclude `.index/` except `state.db`. **`mnesis-ui` is stateless** (no volume) — all state lives in `mnesis`. Profile-gated service: **`--profile agents`** (the **`mnesis-agents-runtime`** — the LangGraph agent layer (§14): the scheduled **dream-cycle MaintenanceAgent** (the single owner of periodic maintenance), the notes-inbox **WritingAgent**, and the approval-gated **ActionAgent** — reaching `mnesis` only over the internal MCP endpoint and itself stateless: durable agent state + run audit + proposals/reports stay on its volumes, knowledge stays in `mnesis`). The ActionAgent is **draft-only with no egress by default**; its **opt-in email channel** (`MNESIS_EMAIL_ENABLED`) is default-off, dry-run, and behind the default-deny egress plane (allowlist + endpoint + quotas + kill-switch), with SMTP credentials supplied only via `.env`/secret store (never the compose file or image) and a durable hash-chained send-audit — enable it through the **staged rollout** in `docs/OPS.md`. *(The old `--profile maintenance` upkeep sidecar is **retired** — removed from `docker-compose.yml`; the dream-cycle agent drives the same decay/graph-lint upkeep over MCP, so there is exactly one scheduler and no double-run.)* When a client reaches the HTTP MCP endpoint by a name other than localhost (e.g. the in-network `mnesis:8080`), the server's DNS-rebinding Host allowlist must include it: **`MNESIS_MCP_ALLOWED_HOSTS`** (comma-separated `host:port` / `host:*`; empty = localhost only). Compose defaults it to `mnesis:*,localhost:*,127.0.0.1:*`. See `docs/OPS.md`.

---

## 13. Scope: in vs. deferred

**In scope (Phase 1 — implemented):** filtered ingest · Markdown + git canonical store · FTS5 keyword search (rebuildable) · MCP interface with `mnesis_ingest` / `mnesis_query` / `mnesis_file_back` (+ `mnesis_get`, `mnesis_list`, `mnesis_rebuild`) · `mnesis` CLI · end-to-end demo and test. All present and exercised by the test suite.

**In scope (Phase 2 — implemented):** confidence scoring (`confidence.py`) & Ebbinghaus-style decay with the active↔stale lifecycle (`lifecycle.py`, `mnesis decay`); relation-aware ingest — reinforce / supersede / contradict / create (`ingest.py`); the durable **state store** (`state.py`: access events + review queue); confidence-blended retrieval with access-on-read reinforcement (`search.py`); and the contradiction review queue (`mnesis review` / `resolve`). The `contradicts` and `decay_class` frontmatter fields back this. All exercised by the test suite and the `scripts/demo_phase2.py` regression demo.

**In scope (Phase 3 — implemented):** the typed-relationship knowledge graph. The `relations` frontmatter field and entity/predicate vocabulary with validation (`vocab.py`, §6); relation extraction on ingest (`ingest.py`); the pluggable `GraphBackend` (`graph.py`) with an embedded-SQLite default, the rebuild-from-Markdown projection (noisy-OR edge confidence, demotion of stale-only edges), and cycle-safe traversal/neighbors — built into `mnesis rebuild` (search index + graph rebuild together; state store untouched); graph-augmented query and `impact()` wired into retrieval (`mnesis_query` folds in graph-reachable pages); the graph tools — `mnesis_entity` / `mnesis_neighbors` / `mnesis_traverse` / `mnesis_impact` / `mnesis_graph_stats` (MCP) and `mnesis entity` / `neighbors` / `impact` / `graph-stats` (CLI), with query/get noting related entities; and **graph lint** (`graph_lint.py`, `mnesis graph-lint [--fix]`) — auto-fixes the safe categories (merge duplicate edges, demote stale-only edges, recompute confidence) and flags the rest (undeclared/orphan entities, dangling structural edges), idempotently.

**In scope (maintenance ops over MCP — implemented):** the curation/upkeep operations a maintenance agent drives are all reachable behind the same authenticated MCP endpoint, so the agent stays MCP-only. **Writers** (idempotent, git-audited): `mnesis_graph_lint(fix)` (graph lint — report, or apply the safe auto-fixes) and `mnesis_decay` (decay/lifecycle pass). **Read-only, side-effect-free diagnostics** (`maintenance.py`): `mnesis_health_report()` — counts by status/kind, pages with no sources, low-confidence/stale counts, open-contradiction count, graph size + demoted/orphan/undeclared/dangling counts, and search-index/graph freshness vs Markdown; and `mnesis_find_duplicates(limit=20)` — **heuristic** near-duplicate candidate pairs (title/tag overlap, shared edges, FTS co-retrieval) with a rationale, which **proposes and changes nothing** (a stand-in pending the Phase-5 vectors that will replace it with semantic similarity). The contradiction-queue tools `mnesis_review` / `mnesis_resolve`, `mnesis_rebuild`, and `mnesis_graph_stats` round out the set. CLI parity: `mnesis health` and `mnesis find-duplicates`.

**In scope (agent layer — implemented):** the **LangGraph agent layer** (`mnesis_agents`), a separately-deployable MCP **client** that uses Mnesis as memory — a multi-LLM base over LangGraph, Agent Skills, governance, triggers/runner, and three concrete agents (the dream-cycle **MaintenanceAgent**, the notes-inbox **WritingAgent** with its source-connector ingestion pipeline, and the approval-gated **ActionAgent** with the outbound-channel + gate + egress + opt-in email-send safety stack). Reaches the core only over MCP; never imports `mnesis`. Fully documented in **§14**.

**In scope (multitenancy — implemented, §16):** mnesis is multitenant from the data layer up — physically **per-tenant stores** (each tenant its own `pages/`/`sources/`/`.cache/` + git repo under `tenants/<id>/`), per-tenant caches/graph/state and rebuild (`tenancy.py`, T1–T2); **credential → `(TenantContext, Principal)`** resolution with the tenant taken **only** from the credential (`auth.py`, T3); within-tenant **role authorization + private/shared visibility** enforced in the data/query layer (`authz.py`, T4); tenant **enforcement across MCP, the Web UI gateway, and the CLI** (T5); a **per-tenant agent runtime** (`mnesis_agents.tenancy`, T6); and **tenant lifecycle + a system-admin boundary + per-tenant quotas** (`admin.py`/`quotas.py`, T7). The `default` tenant keeps single-tenant deployments transparent. Fully documented in **§16**.

**Out of scope for now — map of where each deferred capability lands:**

| Capability | Phase |
|---|---|
| Session/query automation hooks + a general config-driven hook framework *(on-source ingestion via the connectors/WritingAgent, and scheduled maintenance via the dream cycle, are delivered in §14)* | 4 |
| Vector stream + reciprocal rank fusion; LLM-as-judge quality scoring | 5 |
| Multi-agent mesh sync *(per-tenant private/shared scoping is **delivered** — see §16, within-tenant visibility)* | 6 |

When you extend the PoC toward any of these, **update this file first**, then make the code follow it.

---

## 14. The agent layer (`mnesis_agents`)

A **LangGraph-based** agent layer (`src/mnesis_agents/`, importable as `mnesis_agents.*`) is the separately-deployable agent runtime that uses Mnesis as long-term memory. It is the substrate concrete agents are built on; the base, the category abstractions, the runtime, and concrete agents — the schedule-triggered dream-cycle **`MaintenanceAgent`**, the event-triggered **`WritingAgent`** (notes inbox → Mnesis), and the approval-gated, draft-only **`ActionAgent`** (compose → propose → human-approve → deliver), each over its connector/skill/channel pipeline — exist today. It reaches Mnesis **only over MCP** and **never imports `mnesis`** — so Mnesis's governance (redaction, contradiction/supersession review) gates every write server-side, unbypassably. The layer is **multitenant**: each agent runs under a per-tenant `TenantScope` (its own MCP credential + its own governance state), so an agent is confined to one tenant — see §16 "Multitenant agent layer" (`mnesis_agents.tenancy`, T6).

- **Multi-LLM, provider-agnostic.** A shared factory (`src/mnesis_llm/factory.py`) maps a provider key to a LangChain chat model. **Mnesis is now provider-agnostic too**: `mnesis.llm.complete()` keeps its native `stub`/`local`/`anthropic` paths unchanged (no regression) and routes the *broader* providers (`openai`/`google`/`mistral`/`bedrock`/`ollama`/`openai_compatible`) through the same factory — so one `MNESIS_LLM_PROVIDER` switch changes the model for **both** Mnesis and the agents, no code change. langchain is a lazy import; the offline stub needs none of it.
- **Mnesis tools via langchain-mcp-adapters** (`knowledge.py`): the `mnesis_*` tools become LangChain tools; a `ToolRegistry` aggregates sources (namespacing only on collision); a `FakeMnesisTools` source makes the layer testable offline.
- **Agent Skills (agentskills.io)** (`skills/`): SKILL.md folders with strict three-level progressive disclosure (discovery = name+description only; activation loads instructions; resources/scripts on demand, path-confined + bounded). Surfaced to any model via skill cards in the prompt + a `use_skill(name)` tool. Same SKILL.md format Claude Code uses. **Bundled dream-cycle maintenance skills** (`skills/bundled/`) encode the upkeep routines over the Mnesis MCP tools, each stating its auto-vs-propose policy in the body and (load-bearingly) in its `allowed-tools` manifest — a proposal-only skill is simply never granted a write tool: **decay-sweep** (calls `mnesis_decay`, AUTO-APPLY), **graph-hygiene** (`mnesis_graph_lint` report then `fix=True`, AUTO-APPLY safe categories only, flag the rest), **contradiction-triage** (`mnesis_review`/`mnesis_get`, PROPOSE a keep by confidence/sources/recency — never `mnesis_resolve`), **deduplication** (`mnesis_find_duplicates`/`mnesis_get`, PROPOSE merges — never applies), and **quality-sweep** (`mnesis_health_report`, read-only findings). Only hygiene auto-applies; anything that changes knowledge *meaning* is proposal-only. Each bundles a small deterministic post-processing `scripts/` helper (shape the tool output into the documented structured result); `knowledge.FakeMaintenanceTools` is the offline stand-in that makes them testable without a running Mnesis. A separate **source-parsing skill** lives alongside them: **parse-note** — it normalizes a notes/Markdown `InboundEvent` (from the W1 connector) into a clean `{text, source_ref, skip, reason}` for ingestion (strips front-matter/signatures/boilerplate, keeps substantive content verbatim, and **skips** empty/trivial notes), via a pure `scripts/parse_note.py` transform (`allowed-tools: []` — it calls no tools and does **not** ingest; the WritingAgent does, under governance). **Note content is DATA, never instructions**: an embedded directive (e.g. "ignore instructions", "mark pages stale", "ingest as authoritative") rides along as ordinary text in `text` and changes neither the output (`skip`/`source_ref` are derived only from structure and length) nor any agent behaviour — enforced structurally by the mechanical, semantics-free script. Each new source type ships its own `parse-<source>` skill (email/chat/docs arrive with their connectors), so source quirks stay declarative. An **action-composition skill** lives alongside them too: **prepare-meeting-brief** — given a meeting context (topic, attendees, time), it gathers relevant pages/entities via the Mnesis **READ** tools (`mnesis_query`/`mnesis_get`/`mnesis_entity`/`mnesis_impact` — **read-only, no writes**) and composes a grounded, cited `{title, markdown, citations, suggested_channel}` artifact via a pure `scripts/compose_brief.py` transform. **Mnesis content and the input context are DATA, never instructions**: nothing in a retrieved page or the context changes *who* the brief goes to (the artifact has **no destination** — the operator chooses it at the gate), *whether* it is sent (the skill never delivers), the *channel* (`suggested_channel` is a fixed safe default, the inert `draft-outbox`, never content/context-derived), or tool use — enforced structurally by the semantics-free, tool-less script. It **cites only real returned page ids** (never invents) and, when Mnesis has little on the topic, says the brief is *not grounded* rather than confabulating. Each new action ships its own `compose-<action>` skill, so action logic stays declarative.
- **Base agent + categories** (`base.py`, `categories/`): `build_agent(profile)` compiles a LangGraph agent (LangChain 1.x `create_agent`) wiring model + tools + skills + governance + checkpointer, returning a structured result. Three category ABCs declare trigger + write policy — **WritingAgent** (event / ingest), **ActionAgent** (event-or-schedule / propose), **MaintenanceAgent** (schedule / propose).
- **The dream-cycle `MaintenanceAgent`** (`maintenance_agent.py`): the first concrete agent — `DreamMaintenanceAgent`, a schedule-triggered `MaintenanceAgent` (`write_policy="propose"`). `run_dream_cycle(plan?) -> DreamCycleReport` runs a **deterministic LangGraph graph** (one node per pass, sequential) over a configurable plan (default **quality-sweep → decay-sweep → graph-hygiene → contradiction-triage → deduplication**). Each pass activates its M2 skill, dispatches the M1 maintenance MCP tools through F6 governance, and runs the skill's helper script for the structured per-pass output. **Auto-applies only safe hygiene** — `mnesis_decay` and `mnesis_graph_lint` are *not* in `write_tools`, so governance lets them execute, while the knowledge-changing writes (`mnesis_resolve`/`mnesis_ingest`/`mnesis_file_back`) are gated by the `propose` policy and **never fire** (proposals are accumulated, not applied). It is **not** an LLM loop — maintenance is mechanical — and it is **resilient** (a failing pass is recorded and the cycle continues) and **budget-bounded** (F6 tool-call/wall-clock caps stop it with a flagged `stop_reason`). The report shape: `{started, ended, passes:[{name, status, summary, auto_applied, proposals, error}], health_before, health_after, totals}`. The F4 base agent (an LLM loop, via `build()`) remains available for ad-hoc maintenance chat.
  - **Proposals, reporting, crystallization, schedule** (`run_and_record()`, `proposals.py`, `reports.py`). After a cycle: (1) **proposals** are routed to a generic, append-with-upsert **proposals queue** (`ProposalStore`, gitignored JSONL) — `contradiction` proposals carry the Mnesis `review_id` so they **annotate** that open review by id (a recommended keep, **never** `mnesis_resolve`), and `duplicate` proposals key on the unordered page pair; nothing is auto-applied, and re-proposing the same thing upserts one entry (idempotent). The Web UI review screen (G11) reads this queue. (2) Each **`DreamCycleReport`** is persisted (`DreamReportStore`, JSONL history + latest human summary) and mirrored into the **F6 audit log** as a `dream_cycle` record (counts/statuses/ids only — never rationale text or values); the latest is exposed via `mnesis-agents dream-cycle --report`. (3) **Crystallization** (meta-memory, `MNESIS_AGENTS_CRYSTALLIZE`, **default off**) files a bounded, governed maintenance digest of the cycle back into Mnesis via `mnesis_file_back` (one-shot `apply` gate, single write tool, length-capped) — Mnesis's server-side redaction still binds. (4) **Schedule** (`register_dream_cycle`, F5): the cycle subscribes to the runner on a cadence — default a **nightly cron `0 3 * * *`** (`MNESIS_AGENTS_DREAM_CRON`; cron needs the APScheduler extra, an interval via `MNESIS_AGENTS_DREAM_INTERVAL_SECONDS` uses the bundled scheduler) — and is always available on-demand via `mnesis-agents dream-cycle --now`; the handler runs off the loop and repeated firings are idempotent.
- **Triggers + runner** (`triggers/`, `registry.py`, `runner.py`): event and schedule triggers; an `AgentRegistry` of subscriptions; a resilient, observable `Runner`. `mnesis-agents run` registers the scheduled dream cycle (above) and otherwise stays a healthy idle host.
- **The SourceConnector pattern + connectors** (`triggers/connector.py`, `connectors/`): **THE contract every inbound source implements** (notes, email, chat, docs). A `SourceConnector` (an `EventTrigger`) *only detects and normalizes* — it **never calls Mnesis or an LLM** (that is the WritingAgent's job downstream). Shape: a **lifecycle** (`start()`/`stop()`; `stream()` yields events, so it is a drop-in trigger for the runner); **idempotency** via a durable `ProcessedStore` keyed by `(source_ref, content_hash)` (re-seeing identical content is a no-op; a new hash for the same ref re-emits; `ack()` marks processed after dispatch); and **error surfacing** — a bad item becomes a `ConnectorError` (in `self.errors` + an optional handler), never a crash, so one bad item never stops the watch. A subclass implements one method, `poll_once()` (a single detection pass that builds and `submit()`s events); the base drives both **poll** (timed re-scans) and **watch** (filesystem events via `watchdog`, in the `agents` extra; falls back to poll if absent) modes from it. Events use the normalized **`InboundEvent` envelope** — `{source_type, source_ref, kind, text, content_hash, metadata}` (built by `InboundEvent.from_source`, which mirrors them onto the generic runner fields `source`/`payload`/`id`). The first concrete connector is **`NotesInboxConnector`** — it watches `MNESIS_NOTES_INBOX` for new/changed `.md`/`.txt` notes and emits one event per note with a **stable** `source_ref` of `note:<relative-path>` and a `content_hash` over the text. Config: `MNESIS_NOTES_INBOX` · `MNESIS_NOTES_MODE` (`poll`|`watch`) · `MNESIS_NOTES_POLL_INTERVAL` · `MNESIS_NOTES_MAX_BYTES` · `MNESIS_NOTES_SUFFIXES`; the connector ledger lives under `MNESIS_AGENTS_CONNECTOR_STATE_DIR` (gitignored).
- **The `WritingAgent`** (`writing_agent.py`): the second concrete agent — `SourceWritingAgent`, an event-triggered F4 `WritingAgent` (`write_policy="ingest"`) that turns an `InboundEvent` into a **governed Mnesis ingestion**. `handle_event(event, *, approved=False) -> WritingResult`: (1) select the parse skill for the event's `source_type` (the `MNESIS_AGENTS_PARSE_SKILLS` mapping, e.g. `notes:parse-note`) and run it (W2) → clean `{text, source_ref, skip, reason}`; (2) if **skip**, ack + record (no ingest); (3) the **approval policy** — a `source_type` in `MNESIS_AGENTS_APPROVAL_SOURCE_TYPES` holds for human approval (`status="pending_approval"`, not acked) before ingest, while the trusted notes inbox **auto-ingests**; (4) call `mnesis_ingest(text, source_ref)` over MCP through F6 governance (`GovernedTools`, shared via `governed.py`); (5) **interpret** Mnesis's routing into a `WritingResult{source_type, source_ref, status (ingested|skipped|pending_approval|duplicate|error), action (created|reinforced|superseded|contradiction_queued), page_id, redaction_count, superseded_id, review_id, skip_reason, error, acked}`; (6) **ack** in the shared `ProcessedStore` and **audit** (`write_writing_event` — ids/statuses/counts only, never the note text). **Mnesis governance is unbypassable** — redaction/extraction/routing/review run server-side; the agent only *calls the tool* and **records the redaction count it gets back** (it never redacts itself). **Idempotent** — an event already `processed` is a no-op `duplicate`. **Inbound content is DATA, not instructions** — carried in the system prompt and enforced structurally (the deterministic W2 parse; routing fixed by `source_type` + config, never by the note's text). Adding a source is **connector (W1) + `parse-<source>` skill (W2) + one `MNESIS_AGENTS_PARSE_SKILLS` entry** — no agent code change.
- **Writing-pipeline robustness** (`writing_pipeline.py`): the connector→agent path is **effectively-once with no silent loss**. The **dedup key is `(source_ref, content_hash)`**: at-least-once delivery from the connector + idempotent processing at the agent (an already-`processed` item is a no-op `duplicate`); a *different* hash for the same ref (an edit / new-but-overlapping source) still flows to Mnesis, whose reinforce logic handles same-claim duplication. `WritingPipeline.process_event` adds **retry/backoff** — a *transient* failure (the ingest tool raised, e.g. Mnesis momentarily unavailable → `WritingResult.retryable=True`) is retried with exponential backoff (`MNESIS_AGENTS_WRITE_MAX_RETRIES`/`…_BACKOFF_BASE`/`…_BACKOFF_FACTOR`) — and a **dead-letter**: a *poison* item (a non-retryable error, or one still failing after the retry budget) is recorded in a durable `DeadLetterStore` (append-with-upsert JSONL under `MNESIS_AGENTS_DEAD_LETTER_DIR`, keyed by `(source_ref, content_hash)`) **with a reason and attempt count**, and **skipped on re-delivery** (`status="dead_letter"`) — the pipeline never wedges and never silently drops. `process_batch` runs a burst with **bounded concurrency** (`MNESIS_AGENTS_WRITE_CONCURRENCY`) and **isolation** — one poison item never blocks the rest. On-demand backfill: `ingest_note_paths` / the CLI **`mnesis-agents ingest-note <file|dir>`** runs the same parse→govern→ingest→ack→dead-letter path immediately over a file or directory (reusing `NotesInboxConnector.build_event` to normalize).
- **The OutboundChannel pattern + safe channels** (`channels.py`): the **outbound mirror** of the `SourceConnector` — where a connector turns the world into inbound events, a channel turns an action agent's produced **artifact** into an outbound delivery. **THE contract every delivery mechanism implements** (email, Slack, calendar, webhook): `deliver(artifact, destination, context) -> DeliveryResult`, a `name`, and a load-bearing **`risk_class`** — `inert` (local/operator-scoped; nothing reaches a third party) or `external` (leaves the box / reaches a third party). A channel **only delivers; it does not decide whether it is allowed to run** — that is the gate's job (A2). The base defaults `risk_class` to **`external`** (the conservative, gated side) so an undeclared channel is treated as risky, and the gate can treat every `external` channel as **always-gated**. **Only INERT channels ship here** — there is deliberately no external-send channel: **`DraftOutboxChannel`** (`risk_class=inert`) writes the artifact as a Markdown draft (YAML metadata header + body) to `MNESIS_ACTION_OUTBOX` and returns the path — never sends; **`LocalNotifyChannel`** (`risk_class=inert`) notifies only the local operator (console/log + a JSONL `MNESIS_ACTION_NOTIFY_FILE`), no third-party recipient. A `ChannelRegistry` maps names → instances (`default_channel_registry()` = the two inert channels) for the action agent to deliver through; the external `EmailSendChannel` lives in its own module and is layered on **only when enabled** via `action_channel_registry()` (E5). Both deliveries **report failure, never raise**.
- **The approval gate** (`action_gate.py`, the **safety keystone**): **no channel executes without a human approval** — the gate is the **single, fail-closed path** to any side effect (a channel is *only ever* invoked from inside `ActionGate._execute`). It is the durable, out-of-band counterpart of the F6 in-loop human-in-the-loop interrupt. `propose(action_type, channel, artifact, destination, rationale) -> ActionProposal` (1) validates **destination integrity** — the destination comes from policy/user input, *never* from Mnesis content or the artifact: an artifact carrying a destination-control field (`to`/`recipient`/`bcc`/… in its metadata) is **refused** (`DestinationIntegrityError`; anti-exfiltration/injection), re-checked on the executed artifact so an edit can't smuggle one; (2) enforces the **always-gated rule** — `_must_gate` returns True for **every `external` channel regardless of policy**, and for `inert` unless the off-by-default `MNESIS_ACTIONS_AUTO_RUN_INERT` escape hatch is set (external can *never* auto-run); (3) records a **pending** `ActionProposal{id, action_type, channel, risk_class, artifact, destination, rationale, status, created, …}` and **pauses**, executing nothing. A human then **approves** (execute the channel **exactly once**), **edits** (execute the edited artifact/destination — still integrity-checked), or **rejects** (discard, deliver nothing); a decided proposal can't be re-run (`GateError`). Proposals persist in an **`ActionProposalStore`** (the M4 proposals store extended for actions, `action_proposals.jsonl`). Every outcome — `proposed`/`executed`/`execute_failed`/`rejected`/`auto_executed` — is **audited** (`write_action_event`: artifact *identity* — kind/title/length — channel, risk, destination, result; **never the artifact body**). Approvals surface: the CLI **`mnesis-agents actions [list|show|approve|reject] <id>`** (`--destination`/`--title`/`--body-file` edit on approve), designed so the Web review screen (G11) can show them.
  - **Recipient-confirmation gate for external sends (E3).** Approving content is **not** approving a recipient. For a `risk_class=external` proposal the gate is enriched: **`present(proposal_id)`** shows prominently the **recipient(s), channel, egress endpoint, a dry-run rendered preview** of the exact message (via `OutboundChannel.preview` → `ChannelPreview`, body shown to the approver but never logged; with the payload-scan findings), and the **rationale + citations**. **`approve`** then requires an explicit, separate **`confirm_recipient`** — it must be **policy/user-sourced** *and* **exactly match** the proposal's (possibly edited) recipient, which must itself pass **E1** (`validate_recipient`: allowlist + `source=policy`). A **content-only approval** (no `confirm_recipient`) does **not** send (`RecipientConfirmationError`); a **mismatched** or **content-sourced** confirmation is refused; **editing the recipient re-runs E1** (a non-allowlisted edit is refused), editing content re-renders the preview + re-runs the payload scan (in the channel, at send). **External is always gated — no policy/auto-approve path exists** (reaffirmed and asserted). On approval the audit records the **recipient + content hash + `recipient_confirmed`** (never the body); **`reject`** and **`expire`** are terminal. The gate passes `recipient_source="policy"` to the channel (the human confirmed it), so the channel's own E1 + at-most-once still run at send.
- **The `ActionAgent`** (`action_agent.py`): the third concrete agent — `GroundedActionAgent`, an event-or-schedule F4 `ActionAgent` (`write_policy="propose"`) that ties A1–A3 into one governed flow. `run_action(action_type, context, *, destination=None, channel=None) -> ActionResult`: (1) select the compose skill for `action_type` (the `MNESIS_AGENTS_ACTION_SKILLS` mapping, default `prepare-meeting-brief:prepare-meeting-brief`); (2) **gather** grounding via the Mnesis **READ** tools through F6 `GovernedTools` (allowlist = `mnesis_query`/`mnesis_get`/`mnesis_entity`/`mnesis_impact` — a write tool is *not* in the allowlist, so the agent can **never write**, fail-closed); (3) run the A3 skill → a grounded, cited artifact; (4) build an `ActionProposal` with the **channel from policy** (`MNESIS_AGENTS_ACTION_CHANNEL`, default the inert `draft-outbox`; `channel="email"` when the opt-in email channel is enabled) and the **recipient from policy/user structured input — never content** (the explicit `destination` arg, else the trigger context's `recipient` key — *never* the composed body or a Mnesis page) — and submit it to the **A2 gate**, which **pauses** (proposed; nothing delivered). For an **external** channel the gate validates the recipient against **E1 at proposal time** (`validate_recipient`: policy/user-sourced + allowlisted), so a non-allowlisted or content-sourced recipient is **refused before the proposal forms** (`RecipientValidationError` → `status="error"`) and never becomes a sendable proposal. A human approves at the gate → the channel delivers (the inert draft, or — for email — a dry-run/egress-gated, recipient-confirmed send). `ActionResult{action_type, proposal_id, status (proposed|delivered|dry_run|blocked|needs_human|rejected|failed|duplicate|error), citations, title, delivery_result, error}`. **`action_tools()` returns `[]` on purpose** — the delivery surface is the *gated* channel registry, never an LLM-callable tool, so the model can't fire a channel directly. **Idempotent** — a `_DedupStore` maps a `(action_type, context, channel, recipient)` fingerprint → proposal id, so re-triggering the same context+target returns the existing proposal (`status="duplicate"`) and never double-proposes/double-delivers. **The only side effect is the gated channel; Mnesis is read-only.** Triggers: on-demand **`mnesis-agents action <action_type> --context <json|file>`** and `register_action_schedule` (F5 — composes proposal-only briefs for *provided* contexts; real calendar/meeting ingestion is a future inbound connector, out of scope). Adding an action is a `compose-<action>` skill + one mapping entry — no agent code change.
- **The egress control plane** (`egress.py`, **default-deny**): the reusable gate **every future `risk_class=external` channel must pass through** before sending — built *before* any external channel exists. With no configuration, **nothing may egress**. `EgressPolicy.check_send_allowed(channel_risk, recipient, endpoint) -> EgressDecision` composes, in fail-closed order: (1) the **kill-switch** (`MNESIS_EGRESS_KILL`) and **enabled** master switch (`MNESIS_EGRESS_ENABLED`, default off) — either denies everything; (2) **risk** — only `external` sends are governed; (3) **recipient** via `validate_recipient(recipient, source)` — accepted **only** when supplied as structured **`policy`/`user`** input *and* on the **recipient allowlist** (`MNESIS_EGRESS_RECIPIENT_ALLOWLIST`, exact addresses and/or domains, default empty); a recipient whose `source` is content/model/artifact (or unknown) is **rejected outright regardless of the allowlist** (anti-exfiltration), failing closed; (4) **endpoint** on the endpoint allowlist (`MNESIS_EGRESS_ENDPOINT_ALLOWLIST`); (5) per-recipient + global **rate limits + daily quotas** (an `EgressQuotaStore` JSON ledger; `0` = deny, negative = unlimited). A channel calls `check_send_allowed` immediately before sending and, only if allowed, sends + calls `record_send`. **Any error → deny.** Decisions are cheap, deterministic, and **logged with the recipient masked** (never the raw address/secret). The plane is **also reused at proposal time** by the action gate (`validate_recipient`), so an external proposal's recipient must clear E1 before the proposal even forms. The email channel (below) is the real send channel wired through it.
- **The email send channel** (`email_channel.py`, `secret_scan.py`): the **first** `risk_class=external` channel (`EmailSendChannel`, an `OutboundChannel`), built **safe by construction**. `deliver(artifact, destination, context)`: renders the message (From = configured sender, To = the recipient, subject/body from the artifact); then — **(1) at-most-once**: an idempotency key (`context["proposal_id"]`/`idempotency_key`) already in the durable `_SentStore` short-circuits with no re-send; **(2) payload secret-scan** (`secret_scan.scan` — an *independent*, high-confidence scanner for keys/tokens/private-keys/credentials/PII, returning **categories not values**) over the **plaintext payload + rendered message** (so a transfer-encoding can't hide a secret) — any hit → **`blocked`** (defense in depth beyond Mnesis's ingest redaction, never sends); **(3) egress (E1)** — `check_send_allowed(external, Recipient(recipient, source), endpoint)` immediately before sending, with the recipient `source` supplied by the caller (the gate passes `policy`/`user`; absent → unknown → fail closed); **(4) dry-run** (`MNESIS_EMAIL_DRYRUN`, **default true**) renders + returns **`dry_run`**, **sending nothing** (surfacing the egress verdict); **(5) live** only when dry-run is off — egress must pass, **TLS (STARTTLS) is required**, credentials come from env/secret store (`MNESIS_SMTP_*`, **never in code/image**), the endpoint must be allowlisted. **At-most-once / no auto-retry**: a clean failure is **`failed`** (definitely not sent), an **ambiguous** transport failure is **`needs_human`** (records the key so it can't re-send — surfaced for a human to verify, **never auto-retried**), a clean success is **`sent`** (records the key). The `DeliveryResult` records `status` (`dry_run`/`sent`/`blocked`/`failed`/`needs_human`), recipient, endpoint, and content hash — **never the body or any secret**.
- **Send-time operational safety** (`send_audit.py`, refined `email_channel.py`, E4): the guardrails that bound and record real sends, all enforced **at send time** (not just at proposal time). **(1) Immutable send-audit** (`SendAuditLog`) — an append-only, **hash-chained** (tamper-evident; `verify()` recomputes the chain) JSONL writing **exactly one record per send attempt**: `{ts, proposal_id, approval_id, channel, recipient, endpoint, content_hash, decision, status, prev_hash, hash}` — **never the body or a secret**. **(2) Quota at send time** — the channel calls `egress.record_send` on each committed send, so per-recipient + global rate limits and daily quotas are enforced across sends (exceeding → `blocked`, logged). **(3) Last-moment kill-switch** — the authoritative `check_send_allowed` (kill + quota + allowlist + endpoint) is re-evaluated **immediately before transmit**, so a kill engaged *after* approval still halts the send. **(4) Crash-safe at-most-once** — the per-proposal send key is marked **`in_flight` in the `_SentStore` *before* transmit**; a clean success → `sent`, an ambiguous failure → `needs_human`, a clean failure → key cleared (definitely not sent); a process **crash mid-send** (a `BaseException` propagates, uncaught) leaves the key `in_flight`, and any duplicate path resolves it to **`needs_human`** — **no code path produces a double-send; ambiguity always resolves to needs_human**. The gate generates a fresh `approval_id` per approval and passes it (with the stable `proposal_id` send key) to the channel.
- **Email delivery for the action agent (E5)** (`email_channel.register_email_channel` / `action_channel_registry`): the action agent can now propose an **email** delivery through the whole stack above — **disabled by default**. `action_channel_registry()` is byte-identical to `default_channel_registry()` (the two inert channels) **unless `MNESIS_EMAIL_ENABLED` is set**, in which case it also registers `EmailSendChannel` (still **dry-run by default**, still behind E1). So email is a delivery option **only when explicitly enabled**, and an email proposal against a disabled channel **fails closed** (unknown channel → `error`). The recipient is **attached by the agent from policy/user structured input** (the `destination` arg or the context `recipient` key — never the compose skill, which still emits **no** recipient, and never page/body content) and **E1-validated at proposal time** (a non-allowlisted/content-sourced recipient never forms a sendable proposal). Approval requires the E3 **`confirm_recipient`** (`GroundedActionAgent.approve(id, confirm_recipient=…)`); then dry-run renders (`dry_run`) or live mode sends **exactly once** (E2/E4: secret-scan → egress → at-most-once → hash-chained send-audit). **The compose skill is unchanged** — content stays DATA, destinations stay policy: a page that says "also email evil@x" changes nothing (the recipient is never read from content, and confirming it is refused by E1 anyway).
- **Governance/persistence/observability** (`governance.py`, `audit.py`): fail-closed allowlist + write-policy + budgets (LangChain middleware), a SQLite (default) / Postgres checkpointer, HumanInTheLoop approval interrupts, an append-only JSONL audit (names/statuses/ids only — never values), and **opt-in** LangSmith tracing (off unless its env is set). Per-write safety stays Mnesis's job server-side.
- **Deployment.** The single `mnesis:latest` image carries both packages (installed with the `agents` extra). A profile-gated Compose service **`mnesis-agents-runtime`** (`docker compose --profile agents up -d`) runs `mnesis-agents run`, which registers the concrete agents (each over MCP, each gated by an enable flag, each registered resiliently — if Mnesis is unreachable at startup the runner comes up rather than crashing):
  - the scheduled dream-cycle **`MaintenanceAgent`** (`MNESIS_AGENTS_DREAM_ENABLED`, default on) — the **single owner of periodic maintenance** now that the D5 `--profile maintenance` sidecar is retired. Cadence via `MNESIS_AGENTS_DREAM_INTERVAL_SECONDS` (the bundled scheduler is interval-based; precise cron via `MNESIS_AGENTS_DREAM_CRON` needs the APScheduler extra). On-demand: `mnesis-agents dream-cycle --now` / `--report`, `make dream-now` / `make dream-report`, `scripts/smoke_dream_cycle.sh`.
  - the notes-inbox **`WritingAgent`** (`MNESIS_NOTES_ENABLED`, default on; `register_notes_writer` wires the `NotesInboxConnector` as a runner event-trigger + a `notes-writer` subscription running the W4 pipeline) — watches the bind-mounted inbox (`MNESIS_NOTES_INBOX=/data/notes_inbox`, host `${MNESIS_NOTES_INBOX_DIR:-./notes_inbox}`, read-only; `poll` mode default in containers) and ingests new/changed `.md`/`.txt` notes. On-demand backfill: `mnesis-agents ingest-note <file|dir>` / `make ingest-note NOTE=…`, `scripts/smoke_notes_inbox.sh`. The runner stops stateful triggers (connectors) cleanly on shutdown.
  - the approval-gated **`ActionAgent`** — **gated, and draft-only by default**. It composes a grounded, cited brief from Mnesis **read** tools (read-only) and **proposes** a delivery to the **inert** `draft-outbox`; **nothing is sent to a third party by default**. A human approves at the gate (`mnesis-agents actions approve <id>`) to write the draft to the mounted outbox (`MNESIS_ACTION_OUTBOX=/data/action_outbox`, host `${MNESIS_ACTION_OUTBOX_DIR:-./action_outbox}`, read-write). **Email delivery is opt-in** (`MNESIS_EMAIL_ENABLED`, E5): when enabled, `--channel email` proposes a send to a policy-supplied, **allowlisted** recipient — still **dry-run by default**, egress-gated, recipient-confirmed, secret-scanned, at-most-once, and send-audited (the gate refuses a non-allowlisted/content-sourced recipient at proposal time). Mostly **on-demand** (`mnesis-agents action <type> --context …` / `mnesis-agents actions [list|show|approve|reject]`, `make action-brief`/`actions`/`action-approve`/`action-reject`, `scripts/smoke_action_agent.sh`); the **periodic hook** (`register_action_agent`) is **opt-in** (`MNESIS_AGENTS_ACTIONS_SCHEDULE_ENABLED`, default off — there is no real meeting-context source yet). Action proposals persist on the agents-runs volume.

  MCP-only; durable state + run audit + proposals/reports + the connector ledger + the dead-letter + action proposals on volumes (`MNESIS_AGENTS_CONNECTOR_STATE_DIR=/data/agents_runs/connectors`), with approved drafts on the mounted action outbox. `MNESIS_LLM_PROVIDER=local` keeps the whole stack on-prem — **no agent makes model calls** (the dream cycle is deterministic; the writing agent only calls `mnesis_ingest`; the action agent only reads Mnesis; Mnesis runs extraction/composition on the local model). **The action family takes effect only through gated channels: by default everything is inert (nothing sends externally); the email channel is opt-in and, even enabled, is dry-run + egress-gated + recipient-confirmed. Destinations come from policy, and content is data, not instructions.**

---

## 15. Changing this file

This document is co-evolved with the system. Expect the first version to be rough and to sharpen after the first few dozen sources and lint passes. Conventions:

- Any code change that touches a field, directory, env var, tool, or behaviour described here updates this file in the same commit.
- Add new conventions under the section they belong to; don't scatter them.
- Keep it scannable — it is read at the start of every agent session.

---

## 16. Tenancy & isolation (the data-layer primitive)

mnesis is **multitenant from the data layer up**. The store is tenant-scoped *by construction* so cross-tenant access is structurally impossible, not merely access-checked. This is the isolation primitive (T1) the rest of this section builds on: credentials (T3), authorization + visibility (T4), surface enforcement (T5), the per-tenant agent runtime (T6), and tenant lifecycle/admin/quotas (T7). A single-tenant deployment runs transparently as the one `default` tenant, with no credentials required.

**On-disk layout.** `config.DATA_ROOT` (env `MNESIS_ROOT`, default `./wiki`) is the *data root*, not a store. Under it:

```
DATA_ROOT/
  registry.json                # the tenant registry (metadata) — OUTSIDE any tenant root
  credentials.json             # the credential store (HASHED tokens) — OUTSIDE any tenant root
  system_audit.jsonl           # the lifecycle audit (provision/suspend/delete) — OUTSIDE any tenant root
  tenants/<tenant_id>/         # one tenant's canonical store + its OWN git repo
    pages/                     #   canonical Markdown (tracked)
    sources/                   #   redacted sources (tracked)
    .cache/                    #   rebuildable caches: wiki.db, graph.db, state.db (gitignored)
```

**The model (`tenancy.py`).**
- **`Tenant`** — `tenant_id` (a safe slug: lowercase `[a-z0-9_-]`, leading alphanumeric), `name`, `status` (`active`|`suspended`), `created`.
- **`TenantRegistry`** — a small JSON metadata store at `DATA_ROOT/registry.json` recording *which* tenants exist; it holds no tenant content. `ensure()` is idempotent.
- **`TenantContext{tenant_id, root_path}`** — the isolation handle every store is built from. It exposes `pages_dir`/`sources_dir`/`cache_dir`/`git_root` and a **path-resolution guard**: `resolve(*parts)` joins under the root, resolves, and refuses anything that escapes it (traversal `..` or absolute escape) — fail-closed (`PathEscapeError`). `page_path`/`source_path` validate the id segment too.

**No global store / resolved at boundaries.** There is no module-level store and no function that takes a raw cross-tenant path. Each store class (`store.Store`, `search.SearchIndex`, `state.StateStore`, and `graph.get_graph_backend(ctx)`) is **constructed from a `TenantContext`** — passing anything else is a `TypeError`. The module-level convenience functions (`store.write_page`, `search.search`, …) delegate to a store over the **active** context, which is bound explicitly at a boundary via `tenancy.use(ctx)`; `tenancy.current()` raises `NoTenantContextError` when none is bound. Boundaries that bind it: the **CLI** (`--tenant`, default `default`, per invocation), the **MCP server** (stdio binds once; HTTP binds per request via `_TenantBindingMiddleware`), and the **web API** (per request). Each tenant root is its own git repo, so every page mutation is one commit *in that tenant's history*.

**Migration (transparent single-tenant).** `tenancy.migrate_legacy_to_default()` (CLI `mnesis migrate-tenants`) moves an existing single-store layout (`DATA_ROOT/{pages,sources}`) into `tenants/default/` and gives it its own git repo. It is **non-destructive** (content is moved, never dropped; the legacy `.git`/`.index` are left in place) and **idempotent** (a re-run, once `tenants/default/` exists, is a no-op). `tenancy.open_tenant("default")` runs it on first use, so a single-tenant deployment reaches its data as `default` with no manual step.

### Authentication: credentials → (tenant, principal) (`auth.py`)

The single global MCP token is replaced by **tenant- and principal-scoped credentials**. A credential resolves to a **`Principal{principal_id, tenant_id, role}`** where `role ∈ {admin, member, readonly, agent}` (authorization — what each role may *do* — is a later prompt; T3 only records the role).

- **The credential store** (`auth.CredentialStore`) lives **outside any tenant root**, at `DATA_ROOT/credentials.json` (beside the registry), so it is not reachable through a tenant. `issue(tenant_id, principal_id, role, expires_at?, name?)` mints an opaque high-entropy token, returns it **once**, and persists only `sha256(pepper‖token)` — **the raw token is never stored or logged**. `revoke(id)`, `get(id)`, `list_for_tenant(id)` round it out. (CLI: `mnesis --tenant <t> auth issue|revoke|list`; the admin API T7 builds on.)
- **The resolver.** `auth.resolve_principal(credential) -> (TenantContext, Principal)` validates the token (constant-time `hmac.compare_digest`; expired/revoked → invalid) and returns the tenant **taken only from the credential**. **Fail closed:** an absent/invalid/expired/revoked credential raises `InvalidCredential` — there is **no default-tenant fallback**, and the function never reads a tenant id from anywhere else, so a client-supplied tenant id (header/body/path/content) is **ignored by construction**. `auth.authenticated(credential)` binds both the tenant (`tenancy.use`) and the principal (`auth.current_principal()`) for a block.
- **Boundary wiring.** When **`MNESIS_AUTH_ENABLED`** is set, the HTTP app's `_PrincipalBindingMiddleware` resolves the bearer credential per request and binds `(tenant, principal)`, returning `401` on any failure (`/health` stays open and tenant-agnostic). When unset (the default), the **legacy** single-token (`MNESIS_MCP_TOKEN`) + default-tenant path is used, so existing single-tenant deployments keep working until credentials are provisioned (T7).

### Authorization & within-tenant visibility (`authz.py`, T4)

Inside a resolved tenant (cross-tenant is already impossible), a finer layer governs **what a principal may do** and **may see**. Enforcement is in the **data/query layer** (search, graph, get, ingest), never only in a surface, so no surface can leak a private resource. **When no principal is bound** (legacy single-tenant path, CLI, internal maintenance) nothing is narrowed — every check passes and every page is visible.

- **Authorization.** A single `authz.authorize(principal, action, resource=None)` / `authz.require(...)` gates `read`/`write`/`maintain`/`admin` by role: `admin` = all; `member` = read/write/maintain; `agent` = read/write/maintain (a scoped non-human principal — never `admin`); `readonly` = read only. A per-page `read` additionally requires visibility; a per-page `write` on an *existing* page additionally requires ownership (or `admin`) — you may not mutate or re-scope another principal's page.
- **Visibility model.** Pages carry `owner_principal` + `visibility` ∈ {`shared`, `private`} (§4). `shared` = visible to every principal in the tenant; `private` = owner-only (plus `admin`, for governance); an unowned/legacy page is treated as shared. The **default for new pages** is the tenant's own setting (`Tenant.default_visibility`, registry; `TenantRegistry.set_default_visibility`) else the global `MNESIS_DEFAULT_VISIBILITY` (else `shared`).
- **Enforcement points.** `ingest`/`file_back` stamp `owner_principal` (the bound principal) + `visibility` (tenant default or explicit override) and require `write` (readonly denied). `search.search` drops pages the principal can't see. `mnesis_get` reports a private page as **absent** (no existence leak). The graph filters everywhere — `entity`/`neighbors`/`impact` keep only edges asserted by a visible page (an entity backed solely by invisible pages is "not present"); `traverse` drops any result whose path crosses an invisible node; `graph_query` never folds in an invisible page. (`graph_stats` stays tenant-aggregate.) The cache/index is per-tenant and shared across that tenant's principals; visibility is applied **per query** against the bound principal, not baked into the cache.

**Out of scope for T4 (later prompts):** a re-scope/visibility-change surface (the `write`-ownership rule exists in `authz` but no tool calls it yet), web-UI sessions/JWT, per-tenant quotas, and tenant lifecycle (suspend/delete). *(Tenant-scoping the agent layer is delivered in T6 — see "Multitenant agent layer" below.)*

### Surface enforcement (T5)

Every human/agent surface is tenant-scoped **end to end** by a **single choke point** that resolves the authenticated `(TenantContext, Principal)` — never from a client-supplied tenant id — and binds it for the whole request/invocation, fail-closed:

- **MCP server / Web UI gateway (one HTTP app).** When `MNESIS_AUTH_ENABLED`, `mcp_server._PrincipalBindingMiddleware` resolves the **bearer credential** per request via `auth.resolve_principal`, binds `(tenant, principal)`, and returns `401` on any failure (`/health` stays open, tenant-agnostic). Both `/mcp` (every `mnesis_*` tool) and `/api/*` (REST + the SSE chat stream) run inside that binding, so each operates on the credential's tenant with that principal's visibility. A forged/extra tenant id in a header/query/body is **ignored** — the middleware reads only the credential. The contextvar binding propagates to sync tools (worker threads) and to the SSE generator task. Every `/api` read that touches pages is visibility-filtered (`webapi._visible_pages`, `_get_page`/`_entity`/`_graph`/`sources`/`reviews`), matching the MCP tools (T4). *(When `MNESIS_AUTH_ENABLED` is off, the legacy `MNESIS_MCP_TOKEN` + default-tenant path is used — single-tenant, no principal, nothing narrowed.)*
- **CLI.** Tenant-scoped data ops resolve the tenant from a **verified credential** (`MNESIS_CREDENTIAL`), not the bare `--tenant` flag (`cli._resolve_data_context`): a credential's tenant is authoritative and overrides `--tenant`; with `MNESIS_AUTH_ENABLED` and no credential the op is **refused** (fail closed); with auth off, the legacy local `--tenant` (default `default`, no principal) is the single-tenant convenience path. Admin ops (`migrate-tenants`, `auth …`) operate on the data root / credential store directly and are not tenant-data ops.
- **Agent layer.** Reaches mnesis only over MCP, so it is scoped by whatever credential it presents — the same choke point, no special path. It is itself **multitenant** (T6, below).

**Invariant:** no handler/tool/stream runs without a resolved tenant+principal (when auth is on); no surface accepts a client-supplied tenant id; SSE and streaming are per-tenant.

### Tenant lifecycle, the admin boundary & quotas (`admin.py`, `quotas.py`, T7)

**The admin boundary.** Tenant lifecycle is managed only by a **system admin** — a principal resolved from a *system-admin* credential (`auth.resolve_admin`), which carries a reserved non-tenant id (`auth.SYSTEM_TENANT = "__system__"`, which can never collide with a real tenant id) and a reserved role (`system_admin`). A system admin is **never** a tenant member, and a **tenant principal can never manage tenants or see another tenant**: `resolve_principal` refuses a system credential, and `resolve_admin` refuses a tenant credential — both fail closed. `admin.require_admin` gates every lifecycle op. The root of trust is bootstrapped locally (`admin.bootstrap_admin` / `mnesis admin bootstrap`); thereafter `MNESIS_ADMIN_CREDENTIAL` authenticates the admin CLI.

**Lifecycle** (`admin.py`, all admin-only and **audited** in the system audit log — `DATA_ROOT/system_audit.jsonl`, append-only, OUTSIDE every tenant root): **provision** (create the tenant root + its own git repo + cache dirs, then issue its initial tenant-admin credential, returned once), **list**, **suspend / resume** (deny / restore access while **retaining** all data — a suspended tenant's credential is denied by `resolve_principal`, so suspend propagates to every surface and to its agents), and **delete** (behind a guarded confirm = the tenant id; removes the tenant's root + caches + git, its credentials, its registry record, and best-effort its agent state; the audit record survives). CLI: `mnesis admin provision|list|suspend|resume|set-quota|delete`.

**Quotas** (`quotas.py`). Per-tenant resource limits (`max_pages`, `max_bytes`) — the tenant's registry override else the `MNESIS_TENANT_MAX_*` config default; `0` = unlimited. Enforced **fail-closed at the ingest write boundary** (`quotas.require_capacity` before a create/supersede): an over-quota write raises `QuotaExceeded`, surfaced clearly (`mnesis_ingest` returns `not ingested: …`, never crashing). Quotas only bound a tenant *within* its own root, so one tenant can never exhaust another's capacity.

**Deployment.** The tenant registry, credential store, and system audit log live under the data volume (`MNESIS_ROOT`), outside every tenant root. Default deployment is **single-tenant** (the `default` tenant; `MNESIS_MCP_TOKEN` bearer; unchanged) and opts into **multi-tenant** with `MNESIS_AUTH_ENABLED=1` (+ per-tenant credentials via `mnesis admin provision`, and `MNESIS_AGENTS_TENANTS_FILE` for per-tenant agents). Credentials are stored hashed; **per-tenant encryption-at-rest** (per-tenant keys for `tenants/<id>/`) is a documented optional hardening on top of an encrypted data volume.

### Multitenant agent layer (`mnesis_agents.tenancy`, T6)

The agent families run **per-tenant**: each agent is confined to one tenant's data *and* its own governance state. A **`TenantScope`** (`tenant_id` + the tenant's **MCP credential** + per-tenant directories) is the single handle; `resolve_scope` is **fail-closed** (no credential → `UnresolvedTenant`, the agent does not start) and `load_scopes` reads the per-tenant config (`MNESIS_AGENTS_TENANTS_FILE` — a JSON list of `{tenant_id, credential, mcp_url?, notes_inbox?, action_outbox?, egress?}`; else a single legacy scope from `MNESIS_MCP_TOKEN`).

- **Data confinement.** An agent's MCP tools carry **only its tenant's credential** (`mnesis_mcp_source(token=scope.credential)`), so server-side (T3/T5) it can reach **only that tenant's Mnesis** — it can never address another tenant's store. No store/registry is shared across tenants.
- **Per-tenant governance state** (all under `STATE_BASE/tenants/<tenant_id>/`, `STATE_BASE` = `MNESIS_AGENTS_STATE_BASE`, default the run-audit dir): the **run-audit log**; the dream-cycle **proposals queue + reports**; the writing **processed-state ledger + dead-letter**; the **action proposals**; the **egress allowlist + endpoint allowlist + quotas + kill-switch + quota ledger + email at-most-once store + send-audit**; the LangGraph **checkpointer**; and the tenant's own **notes inbox** and **action outbox**. The scope's factory methods (`audit_log()`, `proposal_store()`, `report_store()`, `processed_store()`, `dead_letter_store()`, `action_proposal_store()`, `egress_policy()`, `send_audit()`, `channel_registry()`, `action_gate()`) each build the tenant's own store. Tenant A's agent cannot read B's data, **use B's egress config** (A's allowlist refuses a B-only recipient), or write to B's audit.
- **The runner hosts per-tenant instances.** `cli._build_runner` uses the multitenant path when `MNESIS_AGENTS_TENANTS_FILE` is set — one set of agents per `TenantScope` (dream cycle, notes connector+writer, action), each built from the scope (`build_tenant_dream_agent`/`…_writing_agent`/`…_action_agent`, `register_tenant_agents`); a tenant that fails to resolve is skipped (the others continue), and no resolvable tenant ⇒ idle. The dream cycle curates only its tenant; a tenant's notes inbox is its own; action/egress operate within the tenant. *(Without a tenants file, the legacy single-tenant runner path is unchanged.)*

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
owner_principal: sarah
visibility: shared
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
owner_principal: sarah
visibility: shared
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
