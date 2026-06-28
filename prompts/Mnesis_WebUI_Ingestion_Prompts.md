# Mnesis — Web UI Ingestion Build Playbook

**Add data-ingestion to the web interface: paste or upload sources, preview what Mnesis extracts and how it will file it, curate lightly, and commit — all through a minimalistic, intuitive flow. A sequenced prompt set for Claude Code (Opus 4.8), continuing the Web UI build from G6.**

Until now the web UI is read-only: navigate the graph, read pages, ask questions. Adding knowledge is CLI-only (`mnesis ingest`) or via the `mnesis_ingest` MCP tool. This set extends the UI to cover the write path — the **ingestion** requirements — without turning the clean reading surface into a heavy CRUD console. The design intent is unchanged: the interface helps, then gets out of the way. One obvious "Add" affordance, a preview that builds trust, a single confirm.

Run after the Web UI playbook (G1–G6) and the ingestion pipeline (Phases 1–3).

---

## Architecture decision (read first)

**Split the pipeline into plan and apply.** The current ingest pipeline (Phase 1, extended in Phases 2–3) runs scrub → extract → classify → write in one shot. A trustworthy UX needs to show the user *what will happen before it happens*, so G7 refactors ingestion into:

- **`plan`** — scrub, extract entities/relations, classify routing (new / reinforce / supersede / contradict) against existing pages. **Performs no writes whatsoever** — not even persisting the source. Returns an `IngestPlan`.
- **`apply`** — given a plan plus any user overrides, persist the redacted source and perform the routed write (create / reinforce / supersede / contradict), committing to git and updating the indexes.

This split is the keystone of the whole set. It gives the UI a side-effect-free preview, keeps the human in the loop before any supersession, and surfaces redactions for governance — all without duplicating pipeline logic.

**Browsers still don't speak MCP.** Ingestion endpoints are added to the same REST gateway as G1 (thin adapters over `plan`/`apply`), served behind the nginx proxy. The MCP `mnesis_ingest` tool and the CLI keep working unchanged, now all three surfaces routing through the same `plan`/`apply` core.

---

## Picking up the seams

| Seam (as built) | This set turns it into |
|---|---|
| Phase 1–3 ingest pipeline (one-shot write) | A `plan`/`apply` split enabling preview-then-commit. |
| Redaction runs silently before write | Redaction surfaced in the preview (counts/types, never values). |
| Phase-2 relation classifier (reinforce/supersede/contradict/new) | A visible, overridable routing decision in the UI. |
| Phase-2 review queue (`mnesis review`/`resolve`, MCP only) | A contradiction-review screen. |
| G5 file-back (an existing UI write) | The consistent pattern the ingest flow follows. |
| G1 REST gateway (reads + chat) | Extended with ingestion + review endpoints. |

---

## Tech stack

Continues the Web UI stack — **React 18 + TypeScript + Vite, Tailwind, TanStack Query, the FastAPI gateway, nginx**. New pieces: **TanStack Query mutations** for the write flow, **multipart upload** to FastAPI for files, native drag-and-drop (no new dependency). Synchronous preview for single sources; a simple client-side queue for batches. No new backend services.

---

## Scope boundary

**In scope:** paste-text and file-upload ingestion · side-effect-free preview (extracted page, redactions, entities, relations, routing decision) · light curation before commit (title, tags, accept/reject relations, override routing) · single + batch ingestion · a Sources view · a contradiction-review screen.

**Deliberately deferred (with reasons):** URL-fetch ingestion (needs a fetcher + SSRF guards) · PDF/DOCX ingestion (needs a text-extraction step — G8 leaves a pluggable seam) · free-form editing of canonical pages from the browser (git/lifecycle implications; the ingest-preview curation covers shaping data *as it goes in*) · per-user auth/multi-tenancy (Phase-6 territory).

---

## Optimizing for Claude Code on Opus 4.8

Keep **Opus 4.8** active for the whole set. Same six-part template — **CONTEXT / OBJECTIVE / BUILD / CONSTRAINTS / ACCEPTANCE / ON DONE** — and standing rules: backend testable offline with `MNESIS_LLM_STUB=1`; conventional commits; self-checking acceptance; keep `CLAUDE.md` and README in sync. Run one prompt per turn and review each diff; the `plan`/`apply` split (G7) is deliberately first so every later change stays small and reviewable. Prompts continue the G series.

---

# The Prompts

---

## Prompt G7 — Ingestion pipeline: plan / apply split

```
CONTEXT: The ingest pipeline (Phases 1-3) scrubs, extracts entities/relations, classifies routing, and writes in one shot. To let the UI preview before committing, split it into a side-effect-free planning step and an applying step, reusing the existing scrub/extract/classify logic with no behaviour change to the CLI/MCP outcome.

OBJECTIVE: Refactor src/mnesis/ingest.py to expose plan_ingest() (no writes) and apply_ingest() (writes), and route the existing one-shot ingest through them.

BUILD:
- plan_ingest(raw_text, source_ref) -> IngestPlan, performing NO writes (not even persisting the source):
    * scrub -> redactions: list of {type, kind, count} (never the matched values); the redacted text is carried internally for apply.
    * extract -> draft_page: {title, summary_markdown, body, tags (normalized entity refs), relations ([{s,p,o}] validated), kind}.
    * classify -> routing: {action: new|reinforce|supersede|contradict, target_page_id?, candidates: [{page_id, title, relation_label, confidence}], auto_resolved: bool, margin?}.
    * warnings: e.g. a hard-blocked secret type, or extraction fell back to minimal.
- apply_ingest(plan, overrides=None) -> IngestResult, performing the writes:
    * Honour overrides: edited title/tags, accepted/rejected relation indices, and a forced routing {action, target_page_id}. Validate overrides (a forced target must exist; rejected relations dropped).
    * Persist the redacted source to the sources dir, then perform the routed write (create / reinforce / supersede / contradict) via store + the Phase-2 routing logic, committing to git and updating the indexes/graph.
    * Return {action_taken, page_id, superseded_id?, review_id?, redaction_count}.
- Re-implement the existing one-shot ingest_source() as plan_ingest() followed by apply_ingest(plan) so CLI and MCP behaviour is unchanged.
- Stub mode (MNESIS_LLM_STUB=1) drives deterministic plans so the whole flow is testable offline.

CONSTRAINTS:
- plan_ingest performs ZERO writes and ZERO commits. A previewed-then-abandoned source must leave nothing on disk.
- No new classification logic — reuse Phase-2's. apply must go through store (so writes are committed/audited).
- IngestPlan/overrides are plain serializable dicts (they will cross the HTTP boundary in G8).

ACCEPTANCE:
- tests/test_ingest_plan_apply.py (stub): plan on a source with a fake secret returns redactions (no values), a draft page, and a routing decision, and writes nothing (assert sources dir + git log unchanged); apply then writes exactly one outcome and the secret is absent everywhere; a forced supersede override marks the target stale; rejecting a relation omits it from the written page; the re-implemented one-shot ingest matches prior behaviour (existing Phase-1/2/3 ingest tests still pass). `pytest -q` green.

ON DONE: run tests, commit ("feat(ingest): plan/apply split for preview-then-commit"), report the IngestPlan shape.
```

---

## Prompt G8 — Ingestion & review API endpoints

```
CONTEXT: plan/apply exist. Expose them (and the existing review queue) over the G1 REST gateway so the browser can ingest and resolve contradictions.

OBJECTIVE: Add ingestion, source, and review endpoints to src/mnesis/webapi.py as thin adapters over plan_ingest/apply_ingest and the Phase-2 review functions, with multipart upload support.

BUILD:
- Ingestion:
    * POST /api/ingest/preview -> accepts JSON {text, source_ref?} OR multipart (file + optional source_ref); returns the IngestPlan. For multipart, read the file as text (a pluggable extractor keyed by content-type: text/markdown handled now; register a clear extension point for PDF/DOCX, returning a friendly "unsupported type" for those until added).
    * POST /api/ingest/commit -> accepts {plan, overrides?}; returns the IngestResult.
    * Enforce a max upload size (config MNESIS_MAX_UPLOAD_BYTES) and validate content types.
- Sources:
    * GET /api/sources -> list ingested sources (id, ingested_at, the page(s) they produced).
    * GET /api/sources/{id} -> the redacted source text + provenance.
- Reviews (wrap the Phase-2/Prompt-13 functions):
    * GET /api/reviews -> open contradictions: id, the two pages (id, title, confidence), conflict detail.
    * POST /api/reviews/{id}/resolve -> {keep_page_id} applies the supersession and clears the queue entry.
- All /api/* writes require the bearer token (reuse MNESIS_MCP_TOKEN); reject otherwise. Errors return structured JSON (code, message) the UI can render.

CONSTRAINTS:
- Routes stay thin: no business logic beyond request/response mapping and the extractor dispatch.
- preview must remain side-effect-free (it only calls plan_ingest).
- Never echo a redacted value back in any response.

ACCEPTANCE:
- tests/test_webapi_ingest.py (stub): preview (text and multipart) returns a plan and writes nothing; commit writes the outcome and returns the page id; an oversized upload and an unsupported content-type are rejected cleanly; sources list/detail return provenance without leaking redacted values; reviews list + resolve supersede the correct page; unauthenticated writes are rejected when a token is set. `pytest -q` green.

ON DONE: run tests, commit ("feat(ui-api): ingestion, sources, and review endpoints"), report the endpoint list.
```

---

## Prompt G9 — Add flow: paste/upload → preview → commit

```
CONTEXT: The ingestion API exists. Build the core ingestion experience: one obvious entry point, a single screen that previews and commits. Minimalistic and intuitive — this is the headline of the set.

OBJECTIVE: Implement the single-source Add flow: an "Add to Mnesis" entry, a paste/upload input, a preview/review panel, light curation, and a confirming commit.

BUILD:
- Entry: a prominent "+" in the left rail and a Cmd/Ctrl-K "Add to Mnesis" action, opening an /add route (or modal). Keep it one focused screen.
- Input: a large text area ("paste a source...") plus a drag-and-drop / click file zone (text/markdown). An optional source name (auto-suggested from filename or first line). A single primary "Preview" action.
- Preview/review panel (from POST /api/ingest/preview), laid out calmly, not as a dashboard:
    * Redaction summary: e.g. "2 secrets, 1 email redacted before storage" - reassuring, counts/types only, never values. If a hard-blocked warning is present, show it prominently.
    * Extracted page: title (editable), summary; tags as entity chips (removable; add-by-typing); relation triples as s -p-> o rows with accept/reject toggles.
    * Routing decision, stated plainly and prominently: "Will create a new page" / "Will reinforce <page-link>" / "Will supersede <page-link> (marks it stale)" / "Conflicts with <page-link>". Show candidate matches with confidences. Allow override: force New, or pick a target to reinforce/supersede. Supersession override shows an explicit confirm ("this marks <page> stale").
    * Primary "Add to Mnesis" commits via POST /api/ingest/commit with overrides (edited title/tags, accepted relation indices, forced routing).
- Success state: what happened ("Created <page>" / "Reinforced <page>" / "Superseded <old>, created <new>"), with a link to open the page (and a "view in graph" link). Offer "Add another" to reset.
- Loading/error states styled per the design system; preview is a mutation with a clear spinner; commit disabled until a preview exists.

CONSTRAINTS:
- Nothing is written until the user clicks the commit action - preview is side-effect-free, reflect that (no "saving..." on preview).
- Keep curation light: title, tags, relation accept/reject, routing override. NOT full body editing.
- Use the shared design tokens; entity chips and confidence styling match the rest of the app.

ACCEPTANCE:
- With the stub backend: pasting a source previews extracted entities/relations + a routing decision + a redaction summary; editing tags and rejecting a relation are reflected on commit; committing a "new" source creates a page reachable via the success link and present in /pages; forcing a supersede on a candidate marks the target stale; uploading a .md file previews the same way. tsc clean; build succeeds.

ON DONE: commit ("feat(ui): single-source ingest flow with preview and curation"), report a walkthrough of paste -> preview -> commit.
```

---

## Prompt G10 — Batch ingestion & Sources view

```
CONTEXT: Single ingestion works. Support adding several sources at once, and give the user visibility into what they have fed in.

OBJECTIVE: Add multi-file batch ingestion with per-item review/status, and a Sources view.

BUILD:
- Batch: dropping multiple files (or adding several pastes) builds a client-side queue. Each item previews independently (sequential or limited-concurrency calls to /api/ingest/preview) and shows a status: previewing / ready / committed / error. An item expands to its full review panel (reuse G9's). Actions: commit-all (ready items) and per-item commit; per-item remove. A compact summary line ("3 ready, 1 conflict, 1 error"). Keep it scannable, not noisy.
- Conflicts/superse­des within a batch are surfaced per item with the same routing UI; nothing auto-commits.
- Sources view (/sources): a filterable list of ingested sources (name, ingested_at, the page(s) produced as links). Selecting one shows the stored redacted source text and its provenance. This closes the loop: "what did I add, and what did it become."

CONSTRAINTS:
- Batch previews must not block the UI; show progress per item and keep the app responsive (bounded concurrency).
- Still no auto-commit: batch commit acts only on items the user has reviewed/marked ready.
- Sources view never displays redacted values (the stored source is already redacted).

ACCEPTANCE:
- With the stub backend: dropping 3 files yields 3 queued items that preview to "ready"; commit-all creates 3 pages; an item whose routing is a low-margin contradiction is flagged and routed to review on commit; the Sources view lists committed sources linking to their pages. tsc clean; build succeeds.

ON DONE: commit ("feat(ui): batch ingestion and sources view"), report the batch summary behaviour.
```

---

## Prompt G11 — Contradiction review screen

```
CONTEXT: Ingestion can produce contradictions that are queued rather than auto-resolved (Phase 2). Give the user a place to resolve them - the write operation that keeps the knowledge base coherent.

OBJECTIVE: Implement a contradiction-review screen over GET /api/reviews and POST /api/reviews/{id}/resolve.

BUILD:
- A /review route (and a small badge in the rail showing the open-contradiction count).
- The queue: each open contradiction as a card showing the two conflicting pages side by side - title, current confidence, key claim/snippet, and the conflict detail - with links to open either page.
- Resolve: "Keep this one" on either side calls resolve with the kept page id; the other is superseded (marked stale) and the entry clears. Show a brief confirm noting the supersession. After resolving, the kept page's confidence recovers (the contradiction penalty lifts) - reflect the updated state.
- Empty state: "No open contradictions" done plainly.

CONSTRAINTS:
- Resolution goes only through the API (which uses the supersession machinery) - no ad hoc page edits from the UI.
- Make the consequence explicit before applying (which page becomes stale).
- Resolved items do not reappear.

ACCEPTANCE:
- With a seeded queued contradiction (via batch/single ingest in stub mode): /review lists it with both pages; resolving keeps one and marks the other stale; the badge count decrements; the resolved entry is gone; the kept page shows recovered confidence in /pages. tsc clean; build succeeds.

ON DONE: commit ("feat(ui): contradiction review screen"), report the resolve flow.
```

---

## Prompt G12 — Wire through Compose & finalize

```
CONTEXT: The ingestion UI works in dev. Make uploads and writes work through the nginx proxy in the Compose stack, and finalize docs and governance framing.

OBJECTIVE: Ensure multipart uploads and write endpoints work behind the proxy, update compose/env, and finalize README/CLAUDE.md and the verification checklist.

BUILD:
- nginx config: allow multipart bodies up to the configured limit (client_max_body_size aligned with MNESIS_MAX_UPLOAD_BYTES), keep proxy_buffering off for the SSE chat path, and ensure the token-injection on /api covers the new write endpoints (POST preview/commit/resolve).
- compose/env: surface MNESIS_MAX_UPLOAD_BYTES in .env.example with a sane default; document it.
- README "Web UI" section: the UI now supports ingestion - add the Add/Batch/Sources/Review flows, and a short governance note: sensitive data is redacted before storage and shown in the preview, every commit is human-confirmed, and writes are audited via git.
- CLAUDE.md: note that the Web UI is now a full read+write human surface (alongside CLI and MCP), all routed through the plan/apply core, and that canonical page editing remains out of scope by design.
- A11y/UX polish: keyboard reachable Add (Cmd-K), focus management in the preview/review, clear error toasts, and consistent empty/loading states across the new screens.

CONSTRAINTS:
- The UI container stays stateless; all state remains in Mnesis.
- Verify multipart + larger bodies actually pass through the proxy (not just in dev).
- No regression to the read-only views or the chat groundedness behaviour.

ACCEPTANCE:
- From a clean checkout: make docker-build && make docker-up && make docker-seed -> in the browser, paste a source and commit it through the proxy (page appears in /pages and the graph); upload a .md file; batch-add several; resolve a contradiction; all via http://localhost:3000. compose ps shows mnesis + mnesis-ui healthy. Read views and chat still work.

ON DONE: commit ("feat(ui): ingestion wired through compose, docs and polish"), report the end-to-end bring-up and write flow.
```

---

## Verifying the ingestion UI (after G12)

1. `make docker-build && make docker-up && make docker-seed` — both services healthy.
2. **Add (single):** paste a source containing a fake API key → the preview shows it was redacted (count, no value), the extracted entities/relations, and a clear routing decision → commit → the new page opens and appears in the graph.
3. **Curate:** on a second source, edit the title, drop a tag, reject a relation → commit → the page reflects exactly those choices.
4. **Reinforce/supersede:** add a source about an existing topic → the preview offers reinforce/supersede against the candidate; committing a supersede marks the old page stale (visible in /pages).
5. **Batch:** drop several files → each previews to "ready" → commit-all creates them; any low-margin conflict is routed to review.
6. **Sources:** the Sources view lists what you added, linking to the pages produced.
7. **Review:** resolve a queued contradiction → the loser goes stale, the badge decrements, the kept page's confidence recovers.
8. **No surprises:** previewing then cancelling leaves nothing on disk; nothing is ever written without an explicit commit.

If all eight hold, Mnesis has a complete human write path — paste/upload, see exactly what will happen, curate, confirm — without compromising the calm reading surface or the governance guarantees.

---

## Notes for running with Claude Code

- Run G7 → G12 in order on Opus 4.8, after the Web UI playbook. G7–G8 are backend (pytest); G9–G11 are frontend (verify in the browser against seeded data); G12 ties it through Compose.
- The review judgement that matters most: **preview is side-effect-free, and nothing is written without an explicit human commit.** If a diff makes `plan`/preview persist anything, or auto-commits a supersession without confirmation, that is the bug — it breaks both the trust model and the governance story.
- The redaction summary is a feature, not decoration: it is how the user trusts that sensitive data was caught before storage. Keep it visible and never let a redacted value reach the response.
- Keep curation light by design. The moment the UI starts editing page bodies directly, you are editing the canonical git layer — deliberately out of scope here; if you want it, it is its own set with its own lifecycle handling.
- PDF/DOCX ingestion slots into the G8 extractor seam (a content-type-keyed text extractor) without touching the rest of the flow — the cleanest first extension when you need it.
