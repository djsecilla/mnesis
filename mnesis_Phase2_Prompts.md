# mnesis — Phase 2 Build Playbook

**Confidence scoring + supersession lifecycle. A sequenced prompt set for Claude Code (Opus 4.6) that continues the build from Prompt 7.**

Phase 1 gave you the compounding loop: filter → ingest → write → index → query → file-back. But it leaves the wiki flat — every claim is equally weighted forever, every source spawns a new page, and contradictions are only flagged. Phase 2 is what stops the wiki becoming a junk drawer: facts now carry **confidence**, new sources **reinforce or supersede** existing claims instead of duplicating them, and stale or contradicted knowledge **decays** and sinks in search.

These prompts pick up exactly at the seams the PoC left in place. Run them in order, one per Claude Code turn, after Phase 1 is green.

---

## Picking up the seams

Each Phase-2 capability lands on a seam already present in the repo:

| Phase-1 seam (as built) | Phase-2 wires it up |
|---|---|
| `source_count`, `last_confirmed` recorded but unused | Inputs to the confidence model (Prompt 9). |
| `store.supersede()` exists, never called | Driven automatically by the ingest relation classifier (Prompt 11). |
| "every ingested source creates a new page" | Replaced by reinforce / supersede / contradict / new (Prompt 11). |
| `status: active \| stale` field defined, never set | Set by the decay & lifecycle job (Prompt 12). |
| Contradictions flagged, not resolved | Auto-resolved when confident; queued for a human otherwise (Prompts 11, 13). |
| `search()` ranks by BM25 only | Blended with confidence; stale pages demoted (Prompt 10). |
| Canonical-vs-cache invariant | Refined into search-index (rebuildable) vs state-store (durable) — see below. |

---

## Refined invariant (read before Prompt 8)

Phase 2 introduces durable runtime state (access counts, the review queue) that cannot be derived from Markdown. The invariant is therefore sharpened, not broken:

- **Markdown is canonical.** All knowledge — claims, sources, supersession links, status — lives in Markdown frontmatter and is committed to git.
- **The search index is a rebuildable cache.** `mnesis rebuild` drops and regenerates it from Markdown at any time.
- **The state store is durable, auxiliary state.** Access events and the contradiction review queue live here. `mnesis rebuild` must **not** clear it.
- **Graceful degradation.** Confidence is computed from Markdown-durable inputs (sources, recency, contradictions) plus an optional access boost from the state store. If the state store is lost, confidence falls back to its Markdown-only value — nothing essential is lost.

---

## Confidence model (spec the prompts build to)

Confidence is a value in `[0, 1]`, **computed, never hand-set**, from stored inputs. The formula is illustrative and its constants live in config — tune freely, but keep the shape.

```
support   = 1 - 0.5 ** source_count          # 1 src .50, 2 .75, 3 .875 (saturating)
retention = exp(-days_since(last_confirmed) / S)   # Ebbinghaus decay, S = stability per decay_class
contradiction_factor = 0.6 ** unresolved_contradictions
access_boost = min(0.10, 0.02 * recent_access_count)   # from state store; 0 if state lost

raw   = (w_s * support + w_r * retention) / (w_s + w_r)   # w_s, w_r configurable (default 1, 1)
conf  = clamp(raw * contradiction_factor + access_boost, 0, 1)
if status == "stale":  conf = min(conf, 0.40)        # hard cap for stale pages
```

**Stability (S, days) by decay class** — architecture decays slowly, transients fast:
`decision`/`architecture` = 365 · `fact` = 180 · `note` = 60 · `transient`/`bug` = 21.
Resolve the class from `kind` and `type:value` tags, overridable per page via a `decay_class` frontmatter field.

**Two clocks, kept separate:**
- *Confidence retention* anchors on `last_confirmed`; a new confirming source resets it to now.
- *Staleness inactivity* anchors on the most recent of any access or reinforcement; a mere read defers staleness but does not, on its own, reactivate an already-stale, contradicted page.

---

## Reusing the standard template & rules

Same six-part template as Phase 1 — **CONTEXT / OBJECTIVE / BUILD / CONSTRAINTS / ACCEPTANCE / ON DONE** — and the same standing rules: everything runs offline with `WIKI_LLM_STUB=1`; conventional commits; self-checking acceptance criteria; keep `CLAUDE.md` in sync in the same commit. Prompts continue the global numbering (8 onward).

---

# The Prompts

---

## Prompt 8 — Schema & state-store foundations

```
CONTEXT: Phase 1 is complete and green. Phase 2 adds confidence and lifecycle, which need new frontmatter inputs and a durable state store that survives index rebuilds. This step lays the foundations only — no scoring or behaviour change yet.

OBJECTIVE: Extend the page schema and Page model with Phase-2 fields, add a durable state store, refine the canonical-vs-cache invariant in CLAUDE.md, and keep the whole existing suite green.

BUILD:
- Update CLAUDE.md:
    * Add frontmatter fields: contradicts (list[str], default []) — ids of pages this page conflicts with; decay_class (str|null, default null) — optional override of the class inferred from kind/tags.
    * Clarify status semantics: active vs stale, and that stale pages are demoted in search, never deleted.
    * Add a "Search index vs state store" section stating the refined invariant: Markdown is canonical; the search index is a rebuildable cache; the state store (access events + review queue) is durable and is NOT cleared by rebuild; confidence degrades gracefully to its Markdown-only value if state is lost.
    * Move "confidence scoring & decay; supersession lifecycle" from the deferred table into an "in scope (Phase 2)" section.
- Update src/mnesis/store.py Page model with the two new fields, defaulted so existing pages still parse (backward compatible). Reading a Phase-1 page must not fail.
- Create src/mnesis/state.py: a durable SQLite store at wiki/.index/state.db (gitignored, but conceptually separate from the search index). Tables:
    * access(page_id PK, access_count int, last_accessed iso)
    * review_queue(id PK, page_a, page_b, kind, detail, status [open|resolved], created)
  Functions: record_access(page_id); get_access(page_id) -> {count, last_accessed} | None; enqueue_contradiction(page_a, page_b, detail) -> id; list_open_reviews(); resolve_review(id). The state store must be created on demand and must be untouched by search.rebuild().

CONSTRAINTS:
- No confidence computation, no ingest changes, no search changes yet.
- Backward compatibility: every existing test must still pass unchanged.

ACCEPTANCE:
- tests/test_state.py: record an access twice -> count is 2; enqueue a contradiction -> appears in list_open_reviews; resolve it -> no longer open. tests/test_store.py still passes with the new fields defaulted. `pytest -q` green.

ON DONE: run tests, commit ("feat(phase2): schema fields and durable state store"), report.
```

---

## Prompt 9 — Confidence model

```
CONTEXT: The schema and state store exist. Implement the confidence computation exactly per the model spec in the Phase-2 playbook (support, Ebbinghaus retention, contradiction factor, access boost, stale cap, decay classes).

OBJECTIVE: Implement src/mnesis/confidence.py: a deterministic, configurable function that computes a page's confidence from its Markdown inputs plus optional access state, with a transparent breakdown.

BUILD:
- Decay-class config in config.py: STABILITY_DAYS = {decision:365, architecture:365, fact:180, note:60, transient:21, bug:21}; weights W_SUPPORT, W_RETENTION (default 1, 1); STALE_CAP (0.40); access-boost cap. All overridable via env.
- resolve_decay_class(page) -> str: page.decay_class if set, else infer from kind and type:value tags (a decision:/architecture: tag wins; bug:/transient: -> fast; default by kind).
- compute_confidence(page, access=None) -> (score: float, breakdown: dict): implement the formula precisely; breakdown returns support, retention, contradiction_factor, access_boost, stale_capped (bool) for explainability. Pure given its inputs (now() injectable for tests).
- Clamp to [0,1]; apply the stale cap when page.status == "stale".

CONSTRAINTS:
- No I/O and no LLM calls — pure computation over the passed-in page and access dict.
- Constants live in config; the function reads them, so tuning needs no code change.
- Document the formula in the module docstring, matching CLAUDE.md.

ACCEPTANCE:
- tests/test_confidence.py with injected clock: a fresh single-source fact scores moderate; adding sources raises it; aging past one stability period roughly halves retention; one unresolved contradiction multiplies by ~0.6; a stale page is capped at 0.40; the breakdown sums/derives correctly. `pytest -q` green.

ON DONE: run tests, commit ("feat(phase2): confidence model"), report a small table of example scores.
```

---

## Prompt 10 — Confidence-aware retrieval + access tracking

```
CONTEXT: Confidence can now be computed. Make retrieval use it, and make reads feed back as reinforcement.

OBJECTIVE: Blend confidence into search ranking, demote stale pages, surface confidence on hits, and record access on query/get so reinforcement actually happens.

BUILD:
- Extend search.py:
    * On rebuild/upsert, compute each page's confidence via confidence.compute_confidence (passing access from state.get_access) and store it in the search index as a cached column, with computed_at. This cached value is rebuildable for its Markdown-derived part; the access contribution comes from the durable state store.
    * search(query, limit=10, include_stale=False) -> hits with id, title, snippet, bm25_score, confidence, final_score. final_score blends normalized BM25 with confidence (e.g. final = bm25_norm * (0.5 + 0.5 * confidence)); stale pages are excluded unless include_stale=True, and never outrank an active page of comparable match.
    * rebuild() must still NOT clear the state store.
- Wire access tracking: when a page is returned by wiki_get, and for the top hits of wiki_query, call state.record_access(id). Recompute that page's cached confidence so the access boost and deferred staleness take effect.

CONSTRAINTS:
- Confidence lives in the index/state layers, never in Markdown frontmatter (it is derived).
- Keep ranking explainable: return the component scores on each hit.
- Recording access must be cheap and must never block or fail a query.

ACCEPTANCE:
- tests/test_search_confidence.py: of two pages matching a query, the higher-confidence one ranks first; a stale page is excluded by default and included (demoted) with include_stale=True; reading a page increments its access count and nudges its confidence up; deleting and rebuilding the search index preserves access state and reproduces ranking. `pytest -q` green.

ON DONE: run tests, commit ("feat(phase2): confidence-aware retrieval and access tracking"), report.
```

---

## Prompt 11 — Reinforcement & supersession on ingest

```
CONTEXT: This is the core of Phase 2. Replace the Phase-1 simplification ("every source creates a new page") with a relation-aware ingest that reinforces, supersedes, contradicts, or creates.

OBJECTIVE: Upgrade ingest.py so a new source is classified against existing pages and routed to the right lifecycle action, wiring store.supersede() and the state store.

BUILD:
- After scrubbing and extraction, before writing: find candidate existing pages via search.search on the extracted title/key terms (top N, active pages). For each strong candidate, ask the LLM (stubbable) to classify the new info vs the candidate as one of: reinforces (same claim, new support), supersedes (updates/replaces the claim), contradicts (conflicts, no clear winner), unrelated.
- Route:
    * reinforces -> reinforce(existing): append the new source, source_count += 1, last_confirmed = now; optionally enrich the body; single commit "wiki: reinforce <id>". No new page.
    * supersedes -> write the new page, then store.supersede(old_id, new_page) (old -> stale, links both ways).
    * contradicts -> compute both pages' confidence; if the winner's confidence exceeds the loser's by AUTO_RESOLVE_MARGIN (config), auto-supersede the loser; otherwise write the new page, add each other's id to both pages' `contradicts` lists, and state.enqueue_contradiction(...). 
    * unrelated -> create a new page (Phase-1 behaviour).
- The classifier prompt is a named constant: conservative, must justify its label, defaults to "unrelated" when unsure. Stub mode returns a deterministic label driven by a fixture marker so tests can exercise every branch.
- Config: AUTO_RESOLVE_MARGIN (default 0.25), candidate top-N (default 5).

CONSTRAINTS:
- All four branches must be reachable and tested in stub mode.
- Reinforcement updates last_confirmed (resets the retention clock); access does not.
- Never silently delete a contradicted page — supersede (-> stale) or queue it.

ACCEPTANCE:
- tests/test_ingest_lifecycle.py (stub): ingesting a reinforcing source increments source_count without adding a page and bumps confidence; a superseding source creates a new active page and marks the old stale with both links set; a low-margin contradiction creates a page and a review-queue entry and cross-links contradicts; a clear-margin contradiction auto-supersedes; an unrelated source creates a fresh page. `pytest -q` green.

ON DONE: run tests, commit ("feat(phase2): reinforcement and supersession on ingest"), report which branch each fixture exercised.
```

---

## Prompt 12 — Decay job & lifecycle transitions

```
CONTEXT: Pages now have confidence and can be superseded. Add the periodic process that lets knowledge fade gracefully and recover on reinforcement. (The scheduler that triggers this automatically is Phase 4; here it is a command.)

OBJECTIVE: Implement a decay/lifecycle pass that recomputes confidence corpus-wide and transitions pages between active and stale.

BUILD:
- src/mnesis/lifecycle.py:
    * recompute_all() -> summary: for every page, recompute confidence (refresh the cached value in the index). Transition active -> stale when confidence < STALE_THRESHOLD (config, default 0.25) AND inactivity (no access, no reinforcement) exceeds the decay-class inactivity window. Reactivate stale -> active only on reinforcement/supersession-clear, not on a read alone. Each status change is one commit ("wiki: <id> -> stale|active"). Idempotent: a second run with no time change makes no commits.
    * Return counts: scanned, restaled, reactivated, unchanged.
- CLI: `mnesis decay` runs recompute_all and prints the summary.
- MCP: wiki_decay() tool exposing the same.
- Config: STALE_THRESHOLD, INACTIVITY_DAYS per decay class.

CONSTRAINTS:
- Stale means demoted, never deleted (Markdown and history preserved).
- Idempotency: running twice without time advancing changes nothing and creates no commits.
- Status changes go through store (so they are committed and audited).

ACCEPTANCE:
- tests/test_lifecycle.py (injected clock): an aged, unaccessed, low-support page becomes stale; reinforcing it (new source) reactivates it; a recently accessed page stays active; a second immediate run is a no-op (no new commits). `pytest -q` green.

ON DONE: run tests, commit ("feat(phase2): decay job and lifecycle transitions"), report the summary from a sample run.
```

---

## Prompt 13 — Contradiction review queue (human-in-the-loop)

```
CONTEXT: Low-margin contradictions are being queued (Prompt 11). Give the human a way to see and resolve them. High-margin ones are already auto-resolved.

OBJECTIVE: Surface and resolve the contradiction review queue via CLI and MCP, applying resolutions through the supersession machinery.

BUILD:
- CLI:
    * `mnesis review` -> list open contradictions: each shows the queue id, the two page ids and titles, their current confidences, and the conflict detail.
    * `mnesis resolve <review_id> --keep <page_id>` -> supersede the other page with the kept one (or mark the kept page authoritative), clear both pages' mutual `contradicts` entry, and state.resolve_review(review_id). One commit.
- MCP: wiki_review() and wiki_resolve(review_id, keep_id) mirroring the CLI; wiki_query/get results note when a returned page has open contradictions.
- Resolution must clear the contradiction from BOTH pages' `contradicts` lists, restoring the kept page's confidence (the contradiction_factor penalty lifts).

CONSTRAINTS:
- Resolution always goes through store.supersede / status changes — no ad hoc page edits.
- Resolving is reversible in spirit: the superseded page stays as stale history, never deleted.
- A resolved review never reappears.

ACCEPTANCE:
- tests/test_review.py (stub): create a queued contradiction via ingest; `review` lists it; `resolve --keep` supersedes the loser, removes the contradicts cross-link, lifts the kept page's confidence, and empties the open queue; the resolved entry does not return on a later decay run. `pytest -q` green.

ON DONE: run tests, commit ("feat(phase2): contradiction review queue"), report.
```

---

## Prompt 14 — Surface confidence, regression demo, finalize

```
CONTEXT: All Phase-2 machinery exists and is unit-tested. Surface it in the interfaces and prove the end-to-end lifecycle with a regression demo.

OBJECTIVE: Show confidence and status everywhere, add a Phase-2 end-to-end demo and regression test, and finalize docs.

BUILD:
- Surface: wiki_query / wiki_get / the CLI query and get all display confidence (rounded) and status; stale pages are marked. The MCP tool descriptions mention confidence ordering.
- scripts/demo_phase2.py (stub mode, no network), printing each step:
    1. ingest a claim about a project -> page A, moderate confidence.
    2. ingest a second, agreeing source -> A reinforced: source_count 2, confidence rises, still ONE page.
    3. ingest a source that updates the claim -> new page B supersedes A; A goes stale.
    4. query the topic -> B (high confidence) ranks first; A is excluded by default, demoted with include_stale.
    5. ingest a conflicting, low-margin source -> a review-queue entry; resolve it with `mnesis resolve`.
    6. run `mnesis decay` over an aged fixture page -> it transitions to stale.
- tests/test_phase2_e2e.py: the programmatic regression asserting each step above, plus: deleting wiki/.index/ and running `mnesis rebuild` preserves access/review state and reproduces ranking and confidences.
- Update README (verify-Phase-2 checklist; new commands: decay, review, resolve) and Makefile/justfile (decay, review targets). Confirm CLAUDE.md reflects the shipped Phase-2 schema and that the scope table now lists Phase 2 as in scope and Phases 3-6 as deferred.

CONSTRAINTS:
- No network; the demo and tests run with WIKI_LLM_STUB=1.
- Do not regress Phase-1 behaviour: the original end-to-end test still passes.

ACCEPTANCE:
- `python scripts/demo_phase2.py` prints the full lifecycle; the whole suite `pytest -q` passes; the README Phase-2 checklist passes top to bottom.

ON DONE: run tests, commit ("feat(phase2): surface confidence, regression demo, docs"), report a transcript of the demo run.
```

---

## Verifying Phase 2 (after Prompt 14)

1. `make test` — full suite green, offline. Phase-1 tests still pass (no regression).
2. `make demo-phase2` (or `python scripts/demo_phase2.py`) — prints: a reinforced claim (one page, rising confidence), a supersession (old page stale, new page active), confidence-ordered query results, a queued-then-resolved contradiction, and an aged page going stale.
3. Reinforce a real page by ingesting a second agreeing source — confirm `source_count` rises and no duplicate page appears.
4. `mnesis query "<topic>"` — confirm results are ordered by a blend of match and confidence, and stale pages are absent unless asked for.
5. `mnesis review` then `mnesis resolve <id> --keep <page>` — confirm the loser goes stale and the kept page's confidence recovers.
6. `mnesis decay` twice — first run may restale/reactivate; the second run is a no-op (idempotent).
7. Delete `wiki/.index/` and `mnesis rebuild` — search ranking and confidences return; **access counts and the review queue survive** (durable state store). This is the refined invariant in action.

If all seven hold, the wiki now ages, reinforces, and self-corrects — and the seams for Phase 3 (the typed knowledge graph) are the `contradicts` links and the `type:value` tags you have been writing all along.

---

## Notes for running with Claude Code

- Run Prompts 8 → 14 in order, after Phase 1 is green; keep Opus 4.6 active throughout.
- The one architectural judgement to watch in review: **confidence must never be written to Markdown frontmatter** — it is derived and lives only in the index/state layers. If a diff adds a `confidence:` frontmatter field, that is the bug to catch.
- The relation classifier (Prompt 11) is where quality lives. In stub mode it is fixture-driven; when you switch to a real model, give it the smallest capable model and keep its "default to unrelated when unsure" instruction — over-merging is harder to undo than over-creating.
- Tune the confidence constants in `config.py`, not the formula, once you have real pages to look at.
