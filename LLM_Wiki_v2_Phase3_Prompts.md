# LLM Wiki v2 — Phase 3 Build Playbook

**The typed knowledge graph: entity extraction, typed relationships, graph traversal. A sequenced prompt set for Claude Code (Opus 4.6) that continues the build from Prompt 14.**

Phases 1–2 gave you pages that compound and age. But the pages are still flat — connections live in prose, and a query like *"what's the impact of upgrading Redis?"* can only find pages that literally mention Redis. Phase 3 layers a typed property graph over the pages so the system can **walk relationships**: start at an entity, traverse outward through typed edges, and surface everything downstream — including connections no keyword search would catch. The graph augments the pages; it does not replace them. Pages remain the read surface; the graph is for navigation and discovery, and every traversal lands the reader back on readable pages.

Run these prompts in order, one per Claude Code turn, after Phase 2 is green.

---

## Picking up the seams

| Seam from Phases 1–2 (as built) | Phase 3 turns it into |
|---|---|
| `type:value` tags on every page (`project:atlas`, `library:redis`) | Typed **entity nodes**. |
| Relationship vocabulary reserved in CLAUDE.md §6, expressed in prose only | Structured **typed edges** with provenance. |
| Phase-2 `contradicts` links between pages | `contradicts` **graph edges**. |
| `supersedes` / `superseded_by` page links | `supersedes` **graph edges**; stale pages' edges demoted. |
| Phase-2 confidence per page | **Edge confidence**, aggregated across the pages that assert a triple. |
| Search index as a rebuildable cache | The **graph** is a rebuildable cache too — `wiki rebuild` rebuilds both. |

---

## Refined invariant (read before Prompt 15)

Phase 3 extends the two-speed store with a third derived projection. The rules stay consistent:

- **Assertions are canonical.** Entities (`type:value` tags) and relations (subject–predicate–object triples) are recorded in Markdown frontmatter and committed to git. They are knowledge, so they are canonical.
- **The graph is a rebuildable cache.** `rebuild_graph()` regenerates the property graph from Markdown at any time, alongside the search index. `wiki rebuild` rebuilds search **and** graph.
- **Derived scores stay out of Markdown.** Edge confidence and assertion counts are computed from the asserting pages and live only in the graph cache — never in frontmatter, exactly as page confidence does.
- **The durable state store is never cleared by rebuild** (access events, review queue), and neither the search index nor the graph holds anything that can't be regenerated from Markdown + that state.
- **Graceful degradation.** Lose the graph DB → rebuild it from Markdown. Lose the state store → confidence (and thus edge confidence) falls back to its Markdown-only value.

---

## Graph contract (the spec the prompts build to)

**Entity ref format:** `type:value`, lowercase, hyphenated value.
**Entity types:** `person`, `project`, `library`, `concept`, `file`, `decision`.

**Predicates (directed edge types) and their direction semantics:**

```
uses         A -> B   (A makes use of B)
depends_on   A -> B   (A requires B; changing B affects A)
owns         A -> B   (person/team A is responsible for B)
caused       A -> B   (A brought about B)
fixed        A -> B   (A resolved B)
contradicts  A -> B   (A conflicts with B; stored directed, treated symmetric in traversal)
supersedes   A -> B   (A replaces B)
```

**Relation = a triple** `{s: "<entity-ref>", p: "<predicate>", o: "<entity-ref>"}` recorded in a page's frontmatter. The page that carries it is its provenance.

**Edge provenance & confidence (derived, in the graph cache):** for each distinct triple, `source_pages` = the page ids asserting it; `assertion_count` = how many; `confidence` = a noisy-OR over those pages' Phase-2 confidence — `1 - Π(1 - conf_i)` — so several weak sources combine into a stronger edge. Edges whose only support is stale/superseded pages are **demoted and excluded by default**.

**Impact = reverse traversal.** Since `A depends_on B` means changing B affects A, `impact(B)` walks `depends_on`/`uses` edges **backwards** to collect what would be affected by changing B. This is the headline query.

**Augment, don't replace.** Traversal returns entities **and** the pages that assert the connecting edges, so results are always grounded in readable pages.

---

## Reusing the standard template & rules

Same six-part template — **CONTEXT / OBJECTIVE / BUILD / CONSTRAINTS / ACCEPTANCE / ON DONE** — and the same standing rules: everything runs offline with `WIKI_LLM_STUB=1`; conventional commits; self-checking acceptance; keep `CLAUDE.md` in sync in the same commit. Prompts continue the global numbering (15 onward).

---

# The Prompts

---

## Prompt 15 — Graph schema & relation frontmatter (foundations)

```
CONTEXT: Phases 1-2 are complete and green. Phase 3 adds a typed knowledge graph. This step records relations in Markdown (the canonical, durable representation) and validates them. No graph database and no extraction change yet.

OBJECTIVE: Add the relations frontmatter field and the entity/predicate vocabulary contract, update CLAUDE.md, and keep the whole existing suite green.

BUILD:
- Update CLAUDE.md:
    * Define the relations frontmatter field: a list of triples {s, p, o} where s and o are type:value entity refs and p is an allowed predicate. State that entities are the type:value tags (no separate field) and that relations are the structured edges promoted out of the §6 vocabulary.
    * Add the full graph contract: entity types, predicate list with direction semantics, the entity-ref format, and that edge provenance/confidence are DERIVED (graph cache only, never frontmatter).
    * Add the refined invariant: the graph is a rebuildable cache regenerated from Markdown; wiki rebuild rebuilds search and graph; the durable state store is never cleared.
    * Move "entity extraction & typed-relationship knowledge graph" from deferred into an "in scope (Phase 3)" section.
- Update src/llmwiki/store.py Page model: add relations: list[dict] (default []), parsing/serializing cleanly. Backward compatible — existing pages read fine with an empty list.
- Add src/llmwiki/vocab.py: ENTITY_TYPES, PREDICATES constants; normalize_ref(ref) (lowercase, hyphenate, validate type prefix); is_valid_predicate(p); validate_relation(rel) -> normalized rel or raises with a clear message.

CONSTRAINTS:
- No graph DB, no graph queries, no ingest/extraction changes yet.
- Every existing Phase-1/2 test must pass unchanged.

ACCEPTANCE:
- tests/test_vocab.py: valid triples normalize; an unknown predicate or a ref without a valid type prefix is rejected with a clear error; mixed-case/spaced refs normalize deterministically. A page round-trips with a relations list intact. `pytest -q` green.

ON DONE: run tests, commit ("feat(phase3): relation schema and vocabulary"), report.
```

---

## Prompt 16 — Entity & relationship extraction at ingest

```
CONTEXT: Relations can now be stored and validated. Make the ingestion pipeline extract them, so new pages arrive with structured entities and edges, not just prose.

OBJECTIVE: Upgrade extraction in ingest.py so the LLM emits entities (type:value tags) and relations (validated triples), written into the page frontmatter.

BUILD:
- Extend the extraction prompt (the named constant) to also return: tags (type:value entity refs for every entity mentioned) and relations (triples over those refs using only allowed predicates). Keep it disciplined: only assert a relation the source supports; prefer fewer, well-grounded edges over speculative ones; entity refs must reuse existing forms where possible.
- Parse and validate: run every extracted ref through vocab.normalize_ref and every triple through vocab.validate_relation. Drop (and log to findings) any invalid or unsupported triple rather than failing the ingest. Deduplicate relations.
- Write the validated tags and relations into the Page. Reinforcement/supersession from Phase 2 still applies: when reinforcing an existing page, merge in any new, valid relations (union, deduped) and refresh provenance.
- Stub mode returns a deterministic set of entities and relations driven by fixture markers so tests cover entity + edge extraction without network.

CONSTRAINTS:
- Extraction must not invent entities or relationships; conservative by default, consistent with the Phase-1 extraction discipline.
- Invalid triples are dropped with a recorded reason, never written.
- WIKI_LLM_STUB=1 must exercise the full path offline.

ACCEPTANCE:
- tests/test_ingest_relations.py (stub): ingest a source describing "Atlas uses Redis; auth-migration depends_on Redis; Sarah owns auth-migration" -> the page carries normalized tags and three valid relations; an injected invalid predicate is dropped and reported; reinforcing the page later unions in a new valid relation without duplication. `pytest -q` green.

ON DONE: run tests, commit ("feat(phase3): entity and relationship extraction"), report the extracted triples for the fixture.
```

---

## Prompt 17 — Graph store as a rebuildable projection

```
CONTEXT: Pages now carry entities and relations in frontmatter. Build the property graph as a rebuildable projection of that Markdown, with confidence-weighted edges.

OBJECTIVE: Implement src/llmwiki/graph.py: build the typed property graph from the pages, integrate it into wiki rebuild, and provide the core traversal primitives.

BUILD:
- Use KuzuDB (embedded property graph, Cypher) at wiki/.index/graph (gitignored). Verify the installed kuzu Python API before coding and match it. If Kuzu is unavailable in the environment, fall back to a SQLite edge-table backend exposing the SAME primitives, selected via config; document the choice.
- rebuild_graph() -> summary: drop and repopulate from store.list_pages(). Nodes = distinct entity refs (typed). Edges = relations, deduplicated across pages, each carrying source_pages, assertion_count, and confidence = noisy-OR of the asserting pages' Phase-2 confidence. Also project page-level structural edges: supersedes (page->page) and contradicts (page<->page) from frontmatter. Mark edges supported only by stale/superseded pages as demoted.
- Integrate: extend search.rebuild / the `wiki rebuild` path to rebuild the graph too. Neither rebuild clears the durable state store.
- Query primitives: get_entity(ref) -> {type, pages, edges}; neighbors(ref, predicate=None, direction="out") ; traverse(ref, predicate=None, depth=2, include_demoted=False) -> reachable entities with the connecting edges and paths. Traversal is confidence-weighted and excludes demoted edges by default.

CONSTRAINTS:
- The graph holds nothing that is not derivable from Markdown (+ Phase-2 confidence). It is a pure cache.
- Edges from stale/superseded pages are demoted, not deleted, mirroring Phase-2 lifecycle.
- Keep traversal depth-bounded and deterministic for a given corpus.

ACCEPTANCE:
- tests/test_graph.py: build from 3 fixture pages -> expected nodes and typed edges exist with correct assertion_count and a confidence in (0,1); neighbors and depth-2 traverse return the expected entities and paths; an edge asserted by two pages has higher confidence than one asserted by one; making a supporting page stale demotes its edge (excluded by default); deleting wiki/.index/ and rebuilding reproduces the graph identically. `pytest -q` green.

ON DONE: run tests, commit ("feat(phase3): knowledge graph projection and traversal"), report node/edge counts for the fixtures.
```

---

## Prompt 18 — Graph-augmented & impact queries

```
CONTEXT: The graph and traversal exist. Wire them into retrieval so queries can discover connections keyword search misses, and add the headline impact query.

OBJECTIVE: Add entity resolution and graph traversal to the query path, blend graph proximity into ranking, and implement impact(entity).

BUILD:
- Entity resolution: resolve a free-text query to candidate entity refs (match against entity nodes by ref and by the titles/tags of pages declaring them).
- Graph-augmented query: when a query resolves to an entity, expand to graph-reachable pages (depth-bounded) and fold them into results alongside the BM25+confidence hits. Add a graph-proximity boost to the existing ranking blend (a small additive term that decays with hop distance). This is NOT full reciprocal rank fusion - vector search and RRF remain Phase 5; keep the blend simple and explainable, and return the component contributions on each hit (bm25, confidence, graph_proximity).
- impact(entity, depth=3) -> affected entities with paths and grounding pages: reverse-traverse depends_on and uses edges (per the contract, changing B affects whatever depends_on/uses B). Confidence-weighted, demoted edges excluded by default.

CONSTRAINTS:
- A page reached purely via the graph must still be presented with its grounding (which edge/page connected it) — augment, don't obscure.
- Do not regress plain keyword queries that resolve to no entity; they behave exactly as before.

ACCEPTANCE:
- tests/test_graph_query.py (stub): given pages where "auth-migration depends_on Redis" but the auth-migration page never says the word "Redis", a query about Redis surfaces the auth-migration page via the graph, tagged with the connecting edge; impact("library:redis") returns auth-migration (and transitively Atlas) with correct paths; a keyword-only query with no entity match is unchanged. `pytest -q` green.

ON DONE: run tests, commit ("feat(phase3): graph-augmented and impact queries"), report the discovered path for the Redis example.
```

---

## Prompt 19 — Graph surface via MCP & CLI

```
CONTEXT: Graph queries work internally. Expose them to agents and the shell.

OBJECTIVE: Add MCP tools and CLI commands for entity inspection, traversal, and impact, and note related entities on existing query results.

BUILD:
- MCP tools (verify the SDK registration API against the installed mcp package):
    * wiki_entity(ref) -> entity type, the pages that declare it, and its typed edges with confidences.
    * wiki_neighbors(ref, predicate=None, direction="out") and wiki_traverse(ref, predicate=None, depth=2).
    * wiki_impact(entity, depth=3) -> affected entities with paths and grounding pages.
    * wiki_graph_stats() -> node/edge counts by type, demoted-edge count.
  wiki_query / wiki_get results gain a "related entities" note.
- CLI mirrors: wiki entity <ref>, wiki neighbors <ref> [--pred P] [--in], wiki impact <entity>, wiki graph-stats. Human-readable output showing paths and confidences.
- Tool descriptions explain that traversal is confidence-weighted and excludes stale edges by default.

CONSTRAINTS:
- Tools return concise, structured, grounded results (always cite the pages behind an edge).
- No new ranking logic here - reuse Prompt 18's primitives.

ACCEPTANCE:
- tests/test_graph_mcp.py (stub): call the tool functions directly - entity, neighbors, traverse, impact, graph-stats - asserting grounded, correctly-typed results. Manual: `wiki impact library:redis` prints the affected set with paths. `python -m llmwiki.mcp_server` starts cleanly. `pytest -q` green.

ON DONE: run tests, commit ("feat(phase3): graph tools for MCP and CLI"), report the steps to call wiki_impact from Claude Code.
```

---

## Prompt 20 — Graph consistency & self-healing

```
CONTEXT: The graph is built from extracted assertions, so it accumulates noise: undeclared entities, orphans, duplicate or dangling edges, and edges kept alive only by stale pages. Add the lint that keeps it healthy, consistent with the Phase-1 self-healing principle.

OBJECTIVE: Implement a graph lint that auto-fixes what is safe and flags the rest.

BUILD:
- src/llmwiki/graph_lint.py -> report with categories:
    * Undeclared entities: an entity appears in a relation ref but no page declares it as a tag -> flag (and suggest the page that should).
    * Orphan entities: declared but in no edge -> flag (informational).
    * Duplicate edges: same triple collapsed -> auto-merge provenance (should already be deduped; assert it).
    * Stale-only edges: every supporting page is stale/superseded -> auto-demote (already excluded by default; ensure the cache marks them).
    * Dangling structural edges: supersedes/contradicts pointing to a missing page -> flag.
    * Edge-confidence recompute: refresh each edge's noisy-OR confidence from current page confidences.
- CLI `wiki graph-lint [--fix]`: report-only by default; with --fix applies the safe auto-fixes and prints what changed. Tie it into any existing lint entry point.

CONSTRAINTS:
- Auto-fix only the safe categories (merge dupes, demote stale-only, recompute confidence). Everything else is flagged for human review.
- Lint must be idempotent: a second --fix run with no changes does nothing.
- Never delete an entity or edge that still has any active supporting page.

ACCEPTANCE:
- tests/test_graph_lint.py: a corpus with an undeclared entity, an orphan, and a stale-only edge produces the right categories; --fix demotes the stale-only edge and recomputes confidences; a second --fix run is a no-op; flagged categories are never auto-deleted. `pytest -q` green.

ON DONE: run tests, commit ("feat(phase3): graph consistency and self-healing"), report a sample lint summary.
```

---

## Prompt 21 — Demo, regression, finalize

```
CONTEXT: All Phase-3 machinery exists and is unit-tested. Prove the end-to-end graph behaviour and finalize docs.

OBJECTIVE: Add a Phase-3 demo and regression test, surface the graph in docs, and update CLAUDE.md scope.

BUILD:
- scripts/demo_phase3.py (stub mode, no network), printing each step:
    1. ingest sources establishing entities and relations: Atlas uses Redis; the auth-migration depends_on the Redis cache; Sarah owns auth-migration.
    2. wiki rebuild -> build search index AND graph; print graph stats.
    3. ask "impact of upgrading Redis" -> traversal returns auth-migration and (transitively) Atlas, with the connecting paths - a connection the auth-migration page never states in words.
    4. ingest a source that updates a relation -> the superseding page's edges take over; the superseded page's edges are demoted in traversal.
    5. wiki graph-lint --fix -> show a clean report after fixes.
- tests/test_phase3_e2e.py: the programmatic regression asserting steps 1-4, plus: deleting wiki/.index/ and running wiki rebuild reproduces the graph, the search ranking, and confidences, while preserving the durable state store (access + review queue). Phase-1 and Phase-2 end-to-end tests still pass.
- Update README (verify-Phase-3 checklist; new commands: entity, neighbors, impact, graph-stats, graph-lint), Makefile/justfile (rebuild already covers the graph; add graph-stats and graph-lint targets, plus demo-phase3), and confirm CLAUDE.md lists Phase 3 as in scope with Phases 4-6 deferred.

CONSTRAINTS:
- No network; demo and tests run with WIKI_LLM_STUB=1.
- No regression of Phase-1/2 behaviour.

ACCEPTANCE:
- `python scripts/demo_phase3.py` prints the full graph walkthrough including the discovered Redis-impact path; the whole suite `pytest -q` passes; the README Phase-3 checklist passes top to bottom.

ON DONE: run tests, commit ("feat(phase3): graph demo, regression, docs"), report a transcript of the demo run.
```

---

## Verifying Phase 3 (after Prompt 21)

1. `make test` — full suite green, offline. Phases 1–2 still pass (no regression).
2. `make demo-phase3` — prints entities and typed edges, then answers an impact query by **traversing** to a page that never names the entity in prose. That discovery is the whole point of Phase 3.
3. `wiki impact library:redis` — returns what depends on/uses Redis, with the connecting paths and grounding pages.
4. `wiki entity project:atlas` — shows its type, the pages that declare it, and its typed edges with confidences.
5. Ingest a second source asserting the same relation — confirm the edge's confidence rises (noisy-OR) without a duplicate edge.
6. Make a supporting page stale (via Phase-2 decay) — confirm its edges are demoted and drop out of default traversal.
7. `wiki graph-lint --fix` then again — first run cleans, second is a no-op (idempotent).
8. Delete `wiki/.index/` and `wiki rebuild` — search, graph, and confidences all return; **access counts and the review queue survive**. Markdown remains the only source of truth.

If all eight hold, the wiki now has structure as well as memory — and the seam for Phase 4 (automation) is every command you have built: each is a hook waiting to be fired on an event instead of by hand.

---

## Notes for running with Claude Code

- Run Prompts 15 → 21 in order, after Phase 2 is green; keep Opus 4.6 active throughout.
- The invariant to enforce in review: **entities and relations are canonical (Markdown); the graph and all edge confidences are derived (cache).** If a diff writes a `confidence:` onto an edge in frontmatter, or treats the graph DB as a source of truth, that is the bug.
- Verify the installed **Kùzu** API before Prompt 17; the SQLite edge-table fallback exists precisely so the build never stalls on an embedded-engine quirk.
- Keep extraction (Prompt 16) conservative — a wrong edge propagates into traversal and impact results. Over-creating entities is recoverable via lint; over-asserting relations is more corrosive.
- Tune the graph-proximity boost and traversal depth in `config.py`, not in the query code, once you have a real graph to look at.
