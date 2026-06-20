# Mnesis — Web UI Build Playbook

**A slick, modern web interface for Mnesis: graph navigation, page reading, and chat. A sequenced prompt set for Claude Code (Opus 4.6), deployed as part of the Docker Compose stack.**

This playbook adds the human-facing layer. Mnesis so far has two surfaces — the CLI and the MCP server (for agents). The web UI is the third: a browser app for **navigating the knowledge graph**, **reading wiki pages properly rendered**, and **asking questions in a chat grounded in the wiki**. The design intent: the interface helps, then gets out of the way — fast, minimal chrome, keyboard-friendly, no ceremony.

Run after Phase 3 and the Docker playbook (it extends both the HTTP server from D2 and the compose stack from D3).

---

## Architecture decision (read first)

**Browsers don't speak MCP.** MCP stays the agent surface; the GUI gets a small **REST + SSE gateway** mounted in the same HTTP app the MCP server already runs (D2's FastAPI/uvicorn process). Both surfaces call the same internal functions (`search`, `store`, `graph`, `confidence`) — no duplicated logic, one container, one port.

**The frontend is a separate static container.** A Vite-built React app served by nginx, which also proxies `/api/*` to the Mnesis service. The browser only ever talks to nginx, which keeps CORS trivial and the API token out of awkward places.

**Chat = retrieve → synthesize → cite.** The chat endpoint runs the retrieval the system already has (BM25 + confidence + graph proximity), feeds the top pages to the LLM with a grounded-answer prompt, and streams the answer with **page citations**. Good answers can be filed back (the Phase-1 crystallization mechanism) with one click. In stub mode the synthesis is deterministic, so the UI is fully developable and testable offline.

```
Browser ──> nginx (mnesis-ui) ──/api/*──> mnesis service ──> store / search / graph / llm
                 │                              │
            static React app              MCP endpoint (agents, unchanged)
```

---

## Tech stack (chosen, with reasoning)

| Concern | Choice | Why |
|---|---|---|
| Frontend framework | **React 18 + TypeScript + Vite** | Mainstream, fast dev loop, typed against the API. |
| Styling | **Tailwind CSS** | Utility-first, consistent spacing/typography, easy dark mode. Minimal custom CSS. |
| Graph rendering | **Cytoscape.js** (via react wrapper or direct) | Mature, handles styled property graphs (typed nodes, labeled edges, weights) and incremental expansion better than DIY force-graphs; canvas-rendered, fine at this scale. |
| Markdown | **react-markdown + remark-gfm** | Proper GFM rendering of page bodies; sanitized by default. |
| Data fetching | **TanStack Query** | Caching, refetching, loading states without boilerplate. |
| Streaming | **SSE** (`fetch` + ReadableStream) | Simple one-way streaming for chat; no websocket infrastructure needed. |
| API gateway | **FastAPI routes in the existing Mnesis HTTP app** | Already running (D2); one process, one auth story. |
| Serving | **nginx** (multi-stage Docker build) | Static files + `/api` reverse proxy; tiny image. |

Deliberately not chosen: Next.js (no SSR need — this is an SPA against a local API), a graph DB UI (the graph is ours to render), websockets (SSE suffices).

---

## Reusing the standard template & rules

Same six-part template — **CONTEXT / OBJECTIVE / BUILD / CONSTRAINTS / ACCEPTANCE / ON DONE** — and the standing rules: backend testable offline with `MNESIS_LLM_STUB=1`; conventional commits; self-checking acceptance; keep `CLAUDE.md` and README in sync. Prompts are prefixed **G**.

---

# The Prompts

---

## Prompt G1 — REST + SSE gateway for the GUI

```
CONTEXT: The Mnesis HTTP app (from D2) serves the MCP transport and /health. The web UI needs a browser-friendly API. Mount REST + SSE routes in the SAME app, reusing the existing internal functions — no logic duplication with the MCP tools.

OBJECTIVE: Add src/mnesis/webapi.py with read endpoints for pages, search, and graph, plus a streaming chat endpoint that answers questions grounded in the wiki with page citations.

BUILD:
- Mount under /api in the existing FastAPI app (verify how D2 structured the app and extend it):
    * GET /api/pages?status=&kind=&q= -> paged list of page summaries (id, title, kind, status, confidence, updated, tags).
    * GET /api/pages/{id} -> full page: frontmatter, raw markdown body, computed confidence + breakdown, relations, supersession links, open-contradiction flag.
    * GET /api/search?q=&limit= -> ranked hits with component scores (bm25, confidence, graph_proximity) and snippets.
    * GET /api/graph?root=&depth=&include_demoted= -> subgraph as {nodes:[{ref,type,degree}], edges:[{s,p,o,confidence,assertion_count,demoted,source_pages}]}. With no root, return a bounded overview (cap nodes, prefer high-degree/high-confidence). All graph access via the GraphBackend interface.
    * GET /api/entity/{ref} and GET /api/impact/{ref}?depth= -> mirror the Phase-3 primitives.
    * POST /api/chat (SSE): body {message, history}. Pipeline: run the existing hybrid retrieval; take top-N pages; call the LLM with a grounded-answer system prompt (answer ONLY from the provided pages; cite page ids inline as [[page-id]]; say so when the wiki doesn't contain the answer). Stream tokens as SSE events; finish with a final event carrying {citations:[page ids], retrieval:[hits used]}. Stub mode returns a deterministic answer with citations so tests and UI dev run offline.
    * POST /api/fileback {question, answer} -> reuse the existing file-back path (threshold applies); returns the created digest id or the below-threshold reason.
- Auth: reuse MNESIS_MCP_TOKEN as a bearer token on /api/* (same privileged-surface posture as MCP). /health stays open.
- A grounded-answer prompt constant with the citation convention documented.

CONSTRAINTS:
- No new business logic in routes: thin adapters over store/search/graph/confidence/lifecycle functions.
- Chat must NEVER answer from model memory alone: no retrieved pages -> say the wiki has nothing, return zero citations.
- SSE must flush incrementally (no buffering the whole answer).

ACCEPTANCE:
- tests/test_webapi.py (stub): pages list + detail return the expected shapes; search returns component scores; graph returns a valid subgraph for a fixture root and respects include_demoted; chat streams and ends with citations matching pages that exist; fileback above/below threshold behaves; requests without the token are rejected when a token is set. `pytest -q` green.

ON DONE: run tests, commit ("feat(ui): REST+SSE gateway for the web UI"), report the endpoint list.
```

---

## Prompt G2 — Frontend scaffold & design system

```
CONTEXT: The API exists. Scaffold the frontend app and its visual foundation. Design intent: slick, modern, minimal — the UI helps and gets out of the way. Fast loads, keyboard-friendly, no clutter.

OBJECTIVE: Create the React+TS+Vite app under ui/ with Tailwind, routing, a typed API client, the app shell, and the design system.

BUILD:
- ui/ scaffold: Vite + React 18 + TypeScript. Tailwind CSS. TanStack Query for data fetching. React Router with three routes: /graph, /pages (+ /pages/:id), /chat.
- Typed API client (ui/src/api/): typed functions per G1 endpoint incl. an SSE helper for /api/chat (fetch + ReadableStream parser). Base URL from env (VITE_API_BASE, default /api). Token (if configured) injected from runtime config — never hardcoded.
- App shell: a slim left rail with three icons (Graph, Pages, Chat) + a global search box (Cmd/Ctrl-K command palette opening search across pages and entities; Enter navigates). Content area fills the rest. No top banner, no footers.
- Design system: dark theme default with light toggle; near-black background, high-contrast text, ONE accent color (default a warm orange, set via a single CSS custom property so it is trivially re-themeable); Inter or system font stack; generous whitespace; subtle borders over shadows. Entity-type color tokens (person/project/library/concept/file/decision) and status styles (active, stale=muted+badge) defined once and shared by graph, pages, and chat.
- Placeholder pages for the three routes rendering live data minimally (e.g. /pages lists titles) to prove wiring.

CONSTRAINTS:
- Keep dependencies to the chosen stack; no UI mega-frameworks, no Redux.
- All three views must consume the SAME design tokens (colors, type scale) — defined once.
- npm run build must produce a static bundle servable by any static server.

ACCEPTANCE:
- npm install && npm run dev serves the shell; the three routes render; /pages lists real pages from the API; Cmd-K opens the palette and search navigates. npm run build succeeds. Basic type-check (tsc --noEmit) clean.

ON DONE: commit ("feat(ui): frontend scaffold and design system"), report the route map and token file location.
```

---

## Prompt G3 — Graph view: visualize & navigate the knowledge graph

```
CONTEXT: Shell and API are in place. Build the graph experience — the headline view. It must make the typed, confidence-weighted graph legible and let the user wander it without friction.

OBJECTIVE: Implement /graph with Cytoscape.js: typed/styled rendering, click-to-inspect, progressive expansion, impact mode, and deep links.

BUILD:
- Rendering: nodes colored by entity type (shared tokens), sized by degree; edges labeled with the predicate, width/opacity scaled by confidence; demoted edges hidden by default with a toggle; a force-directed layout with sane defaults (cose/fcose) and smooth pan/zoom.
- Navigation: initial load shows the bounded overview from /api/graph. Clicking a node opens a right side panel: entity type, the pages that declare it (linking to /pages/:id), and its edges with confidences. Double-click (or an Expand button) fetches that node's neighbors and merges them into the canvas — progressive exploration, never load-everything.
- Impact mode: a toggle on a selected node that calls /api/impact and highlights the affected subgraph (paths emphasized, everything else dimmed), with a readable list of affected entities + grounding pages in the side panel.
- Search-to-focus: the Cmd-K palette (and a small in-view search) accepts an entity ref or name, centers and selects it, loading it if absent.
- Deep links: /graph?root=library:redis&depth=2 reproduces a view; node -> page links and page -> entity chips (G4) round-trip.
- Empty/loading states styled per the design system; graph re-fetch on demand (a refresh affordance), no polling.

CONSTRAINTS:
- Bounded data always: never request the full graph unbounded; expansion is per-node.
- The graph must stay legible: cap simultaneous nodes (config), offer "collapse distant" when the cap is hit.
- All styling from the shared tokens; no per-view color forks.

ACCEPTANCE:
- With seeded data: the overview renders typed, labeled nodes; clicking shows the entity panel with declaring pages; expanding merges neighbors; impact mode on library:redis highlights auth-migration -> atlas with paths; deep link reproduces the view; demoted-edge toggle works. tsc clean; build succeeds.

ON DONE: commit ("feat(ui): interactive knowledge graph view"), report a short walkthrough of the interactions.
```

---

## Prompt G4 — Pages view: read the wiki properly

```
CONTEXT: The graph is navigable. Now make the canonical layer pleasant to read — pages are the read surface of Mnesis and deserve typography-grade rendering, with the metadata visible but unobtrusive.

OBJECTIVE: Implement /pages (index) and /pages/:id (reader) with rendered Markdown, a quiet metadata header, relation chips, and lifecycle affordances.

BUILD:
- /pages index: a fast, filterable list (by kind, status, tag; text filter) showing title, kind badge, status, confidence (compact bar or number), updated date. Stale pages visibly muted with a badge. Click -> reader.
- /pages/:id reader:
    * Body via react-markdown + remark-gfm, typographically tuned (measure ~70ch, comfortable line-height, styled headings/code/tables) in both themes.
    * A compact metadata header: kind + status badges, confidence with a hover/popover showing the Phase-2 breakdown (support, retention, contradiction factor, access boost), sources, last_confirmed, tags as entity chips.
    * Entity chips and relations: each type:value tag and each relation triple rendered as a chip; clicking an entity chip jumps to /graph focused on that entity; relation chips show s -p-> o with edge confidence.
    * Lifecycle banners: superseded pages show a prominent "superseded by <link>" banner (and supersedes links back); open contradictions show a warning strip linking the conflicting page.
    * Digest pages show their originating question distinctly.
- Wiki-internal links ([[page-id]] if present in bodies) resolve to /pages/:id.
- Keyboard: j/k or arrow navigation in the index; Cmd-K palette reachable everywhere.

CONSTRAINTS:
- Read-only in this iteration: no page editing from the UI (editing the canonical layer has git implications — defer deliberately).
- Markdown sanitized (no raw HTML injection).
- Metadata stays quiet: one compact header, no dashboard noise around the prose.

ACCEPTANCE:
- With seeded data: the index filters correctly and shows confidence/status; the reader renders GFM properly; the confidence popover shows the breakdown; entity chips round-trip to the graph view; a superseded fixture page shows the banner and link; a digest page shows its question. tsc clean; build succeeds.

ON DONE: commit ("feat(ui): wiki page index and reader"), report screenshots-in-words of index and reader.
```

---

## Prompt G5 — Chat: ask Mnesis

```
CONTEXT: Reading and navigation work. Add the conversational surface: ask questions, get streamed answers grounded in the wiki, with citations that link straight into the pages — and a path to crystallize good answers back into Mnesis.

OBJECTIVE: Implement /chat against the G1 SSE endpoint with streaming, inline citations, a grounding panel, and one-click file-back.

BUILD:
- A clean chat pane: user/assistant turns, streaming tokens rendered live, Markdown in answers. Multiline input, Enter to send, Shift-Enter newline. History kept in client state for the session (sent as context to /api/chat).
- Citations: [[page-id]] markers in the stream render as numbered citation chips inline; the final SSE event's citation list renders under the answer as linked page cards (title, kind, confidence). Clicking opens /pages/:id (new pane or route).
- Grounding panel (collapsible): the retrieval hits the answer was based on, with component scores — visible honesty about where the answer came from. When retrieval is empty, the UI states plainly that Mnesis has nothing on this (and does NOT render a confabulated answer — the backend already enforces this; reflect it).
- File-back: a "Save to Mnesis" action on an assistant answer calling POST /api/fileback with the question + answer; show the created digest id (linking to the page) or the below-threshold reason. Filed answers are marked in the transcript.
- Error/edge states: stream interruption recoverable with a retry; token-auth failure surfaced clearly.

CONSTRAINTS:
- The UI must never display an uncited answer as if grounded: no citations -> show the "not in the wiki" state.
- Keep the chat free of agent cosplay: no fake typing indicators beyond the real stream, no personality chrome. It is a query interface.
- Session-local history only (no server-side conversation store in this iteration).

ACCEPTANCE:
- With seeded data (stub LLM): asking about a seeded topic streams an answer with at least one citation chip linking to a real page; the grounding panel lists the hits; "Save to Mnesis" creates a digest visible in /pages; an off-corpus question yields the "not in the wiki" state with zero citations. tsc clean; build succeeds.

ON DONE: commit ("feat(ui): grounded chat with citations and file-back"), report a transcript of the seeded Q&A.
```

---

## Prompt G6 — Dockerize the UI & wire into Compose

```
CONTEXT: The UI works in dev. Ship it as part of the Compose deployment: a static nginx container proxying /api to the Mnesis service, so `docker compose up` brings up the whole system including the GUI.

OBJECTIVE: Add the ui image (multi-stage node build -> nginx), the compose service, env wiring, healthchecks, and docs; prove the full stack comes up clean.

BUILD:
- ui/Dockerfile: stage 1 node:20-alpine -> npm ci && npm run build; stage 2 nginx:alpine serving the static bundle. nginx.conf: SPA fallback (try_files -> index.html), proxy /api/ and the SSE path to http://mnesis:8080 with SSE-friendly settings (proxy_buffering off, read timeouts raised for streams), gzip on static assets.
- docker-compose.yml: service mnesis-ui: build ./ui, depends_on mnesis (healthy), ports "${MNESIS_UI_PORT:-3000}:80", restart unless-stopped, healthcheck on /. The browser talks ONLY to mnesis-ui; the mnesis API port no longer needs host exposure for UI use (leave it published only if agents connect from the host; note the choice).
- Token plumbing: if MNESIS_MCP_TOKEN is set, nginx injects the Authorization header on proxied /api requests server-side (env-templated config), so the browser never handles the token on this trusted-host deployment. Document the tradeoff and that per-user auth is a future iteration.
- Makefile: extend docker-up/down/logs to include the UI; add ui-dev (local vite against a running mnesis).
- README "Web UI" section + OPS.md note: ports, the proxy/auth model, and that the UI is stateless (safe to rebuild anytime).
- Update CLAUDE.md: the system now has three surfaces (CLI, MCP for agents, Web UI for humans) sharing one core.

CONSTRAINTS:
- The UI container is stateless: no volumes, all state lives in Mnesis.
- SSE must work through the proxy (no buffering) — verify explicitly.
- `docker compose up -d` with no profiles brings up mnesis + mnesis-ui both healthy.

ACCEPTANCE:
- From a clean checkout: make docker-build && make docker-up && make docker-seed -> browse http://localhost:3000 -> the graph renders seeded entities, a page renders, and chat streams a cited answer through the proxy. compose ps shows both services healthy. Stopping/removing mnesis-ui loses nothing (stateless).

ON DONE: commit ("feat(ui): dockerized web UI in compose"), report the bring-up sequence and the URL map.
```

---

## Verifying the Web UI (after G6)

1. `make docker-build && make docker-up && make docker-seed` — `mnesis` and `mnesis-ui` both healthy.
2. Open the UI — the **graph** shows seeded typed entities; click a node → entity panel with declaring pages; expand neighbors; impact mode highlights the dependency paths.
3. Open a **page** — properly rendered Markdown, quiet metadata header, confidence breakdown on hover; entity chips round-trip to the graph; a superseded fixture shows its banner.
4. **Chat** — ask about seeded content: the answer streams with citation chips that open the cited pages; the grounding panel shows the retrieval; "Save to Mnesis" creates a digest that then appears in /pages *and* is findable by a follow-up chat question — the compounding loop, now visible in the GUI.
5. Ask something off-corpus — the UI says Mnesis has nothing, with zero citations. No confabulation.
6. `docker compose rm -sf mnesis-ui && docker compose up -d mnesis-ui` — the UI returns with nothing lost (stateless).

---

## Notes for running with Claude Code

- Run G1 → G6 in order, after Phase 3 and the Docker playbook. G1 is backend-only (testable with pytest); G2–G5 are frontend (verify in the browser against seeded data); G6 ties it into compose.
- The review judgement that matters most is in **G1/G5: groundedness.** The chat must answer only from retrieved pages and the UI must never present an uncited answer as grounded. If a diff lets the model answer from its own memory when retrieval is empty, that is the bug — it would quietly break the trust the whole system is built to earn.
- Watch the **SSE-through-nginx** detail in G6 (`proxy_buffering off`); it is the classic silent breaker of streaming UIs.
- The accent color and entity-type palette are single-source design tokens (G2) — retheme there, not per component.
- Deliberately deferred for later UI iterations: page editing from the browser (git implications), per-user auth/multi-tenancy (Phase 6 territory), saved chat history, and a review-queue UI for Phase-2 contradictions — that last one is the most natural next addition and slots in as a fourth route over the existing /api surface.
