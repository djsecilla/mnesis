# mnesis (MVP PoC)

mnesis is a knowledge base that **compounds** instead of resetting. Where
retrieval-augmented generation fetches context and forgets it, this wiki
accumulates and reinforces it: sources are filtered, ingested, and written as
canonical Markdown pages; those pages are indexed for keyword search; queries
draw on them; and synthesized answers are filed back as durable, retrievable
knowledge. This repository is the **Phase-1 MVP proof of concept** demonstrating
that end-to-end loop — *filter → ingest → write → index → query → file back →
query again and see it surface*. The design contract lives in
[`CLAUDE.md`](CLAUDE.md), the authoritative schema document for the system.

## Architecture in one paragraph

The **Markdown pages under `wiki/pages/` are the single source of truth**, each
a YAML-frontmatter document versioned in git (every write is one commit — git is
the audit trail). A source is ingested by first **scrubbing** secrets/PII at the
boundary (`filters.py`), persisting the redacted source for provenance
(`wiki/sources/`), calling an **LLM** (`llm.py`, with a deterministic offline
stub) to extract a disciplined `{title, summary, key_facts, tags}`, and writing
a canonical `fact` page (`store.py`, `ingest.py`). A **SQLite FTS5 index**
(`search.py`) is a *rebuildable cache* — a pure projection of the Markdown that
`rebuild()` can reconstruct at any time. Everything is exposed both as a **CLI**
(`cli.py`) and as an **MCP server** (`mcp_server.py`) so Claude Code and other
agents can use it natively. Filing a synthesized answer back
(`mnesis_file_back`) writes a `digest` page — that is the compounding step.

## Prerequisites

- **Python 3.11+** with a `sqlite3` compiled with **FTS5** (the uv-managed and
  python.org CPython builds have it; `make test` will tell you clearly if not).
- **[uv](https://docs.astral.sh/uv/)** for environment and dependency management.
- **git** (the canonical store commits every page mutation).
- *Optional:* `ANTHROPIC_API_KEY` for real LLM extraction. Without it (or with
  `MNESIS_LLM_STUB=1`) mnesis runs fully offline with a deterministic stub, so the
  tests and demo never touch the network.

## Setup

```bash
make setup        # uv venv && uv pip install -e .
make test         # full suite, offline (expect: all passing)
make demo         # end-to-end compounding-loop demo, offline
```

`make help` lists every target (`setup`, `test`, `demo`, `run-mcp`, `rebuild`).

By default the wiki lives under `./wiki` (override with `MNESIS_ROOT`). The SQLite
index under `wiki/.index/` is gitignored — it is a rebuildable cache, never the
source of truth.

## The four core commands

The `mnesis` CLI is installed by `make setup`. The four core verbs:

```bash
# 1. INGEST a source (a file, or - for stdin). --ref sets the provenance id.
echo "Project Atlas uses Redis for caching." | mnesis ingest - --ref atlas-notes
mnesis ingest path/to/notes.md            # --ref defaults to the file stem

# 2. QUERY the wiki (BM25 keyword search).
mnesis query "redis caching"

# 3. GET a page's full Markdown by id.
mnesis get project-atlas-uses-redis-for-caching

# 4. FILE-BACK a synthesized answer as a durable digest page (the compounding
#    step). Files only if the quality score clears MNESIS_FILEBACK_THRESHOLD (0.7).
mnesis file-back "What caches Atlas?" "Atlas uses Redis for caching." --score 0.9
```

Utilities round it out: `mnesis list` (all pages) and `mnesis rebuild`
(reconstruct the search index from Markdown). Run any command under
`uv run mnesis ...` if you have not activated the venv.

### Phase-2 lifecycle commands

Pages carry a derived **confidence** and a **status** (`active`/`stale`); `query`
and `get` display both, and search blends confidence into ranking (stale pages
are hidden unless `--include-stale`). Three commands drive the lifecycle:

```bash
# Recompute confidence corpus-wide; age unread, low-confidence pages to stale.
mnesis decay

# List open contradiction reviews (low-margin conflicts ingest couldn't auto-resolve).
mnesis review

# Resolve one: keep a page, supersede the other (-> stale), lift the kept confidence.
mnesis resolve <review_id> --keep <page_id>
```

Ingest is relation-aware: a new source can **reinforce** an existing page (more
support, no new page), **supersede** it (old → stale), **contradict** it
(auto-resolved by confidence margin, else queued for `review`), or create a new
page. See [`CLAUDE.md`](CLAUDE.md) §7/§8/§11 for the model.

### Phase-3 graph commands

Ingest extracts typed **entities** (`type:value`) and **relations** (`{s,p,o}`
triples) into page frontmatter; `mnesis rebuild` projects them into a knowledge
graph (alongside the search index). The graph is a rebuildable cache behind a
pluggable backend — `MNESIS_GRAPH_BACKEND` selects it (default `sqlite`, an
embedded backend; a Tier-B backend like Postgres+AGE or Neo4j implements the
same interface with no other changes).

```bash
mnesis entity library:redis           # type, declaring pages, and typed edges
mnesis neighbors library:redis --in   # adjacent entities (--in for incoming; --pred to filter)
mnesis impact library:redis           # what depends on/uses it (reverse traversal, with paths)
mnesis graph-stats                    # node/edge counts by type and predicate
mnesis graph-lint [--fix]             # consistency check; --fix applies the safe auto-fixes
```

`query`/`get` also note a page's related entities, and `query` folds in
graph-reachable pages (grounded by the connecting edge) even when they lack the
keyword. See [`CLAUDE.md`](CLAUDE.md) §6 for the graph contract.

## Connect the MCP server to Claude Code

mnesis exposes its tools over the [Model Context Protocol](https://modelcontextprotocol.io):
`mnesis_ingest`, `mnesis_query`, `mnesis_get`, `mnesis_file_back`, `mnesis_list`,
`mnesis_rebuild`, `mnesis_decay`, `mnesis_review`, `mnesis_resolve`, and the graph tools
`mnesis_entity`, `mnesis_neighbors`, `mnesis_traverse`, `mnesis_impact`,
`mnesis_graph_stats`, `mnesis_graph_lint`.

**Run the server standalone** (stdio transport): `make run-mcp` (i.e.
`uv run python -m mnesis.mcp_server`).

**Auto-discovery.** This repo ships a [`.mcp.json`](.mcp.json) registering the
server, so running `claude` from the project root discovers it automatically
(approve the project-scoped server when prompted, then `/mcp` to confirm). It
launches `.venv/bin/python -m mnesis.mcp_server`, so run `make setup` first.

**`claude mcp add` alternative:**

```bash
claude mcp add mnesis -- uv run python -m mnesis.mcp_server
```

Set `ANTHROPIC_API_KEY` for real extraction, or `MNESIS_LLM_STUB=1` to run offline.

**Networked (HTTP) transport.** For container/remote deployment, set
`MNESIS_MCP_TRANSPORT=http` (default `stdio`). The server then serves streamable
HTTP at `/mcp` on `MNESIS_MCP_HOST:MNESIS_MCP_PORT` (default `0.0.0.0:8080`),
plus an unauthenticated `GET /health` returning quick stats (page count, index/
graph present). If `MNESIS_MCP_TOKEN` is set, every tool call must send
`Authorization: Bearer <token>`; if unset, the server logs a warning and the
endpoint is unauthenticated — treat it as privileged (it can ingest and modify
knowledge). Point a client at it with:

```bash
claude mcp add mnesis --transport http http://<host>:8080/mcp \
  --header "Authorization: Bearer $MNESIS_MCP_TOKEN"
```

**Local-first inference (opt-in).** By default mnesis uses Anthropic (or the
offline stub). For a privacy-preserving deployment where **sources never leave
the host**, switch the provider to a local model server via the `local-llm`
compose profile. In `.env` set `MNESIS_LLM_PROVIDER=local`,
`MNESIS_LLM_MODEL=llama3.2:1b` (a small default — change as you like), and blank
`MNESIS_LLM_STUB`, then:

```bash
docker compose --profile local-llm up -d
```

This starts an `ollama` service (model weights on the `ollama-models` volume), a
one-shot job that pulls the configured model, and points `llm.py` at
`http://ollama:11434`. With this profile, **ingestion and extraction make no
external inference calls** — no Anthropic request, no API key needed. A plain
`docker compose up` does **not** start the model service (profile-gated); the
default Anthropic/stub behaviour is unchanged.

**Maintenance sidecar (opt-in).** The wiki needs periodic upkeep — decay, graph
lint, cache freshness. Until Phase 4 moves these into the app as event hooks,
the `maintenance` profile runs them on a cadence at the deployment layer:

```bash
docker compose --profile maintenance up -d        # default interval: daily (MNESIS_MAINT_INTERVAL)
```

The sidecar shares the data volume with the server and, each cycle, runs
`mnesis decay`, `mnesis graph-lint --fix`, and a rebuild-if-missing check — all
through the **CLI**, so every change is committed and git-audited in the volume.
Commands for capabilities not yet built are skipped cleanly, write contention is
retried (WAL), and an empty wiki is a clean no-op. It is **not** started by a
plain `docker compose up`. **Phase 4** replaces this sidecar with in-app
scheduling, at which point it can be retired.

## Run with Docker

A containerized core stack: a single `mnesis` service (HTTP MCP server) over a
persistent volume. See [`docs/OPS.md`](docs/OPS.md) for backup/restore and ops.

**Prerequisites:** Docker (with Compose v2). No Python/uv needed on the host.

```bash
cp .env.example .env          # then edit: MNESIS_MCP_TOKEN (recommended), keys, etc.
make docker-build             # build the image
make docker-up                # start the stack; wait for the `mnesis` service to be healthy
make docker-seed              # ingest bundled sample sources (offline, idempotent)

make docker-cli ARGS='query "redis"'          # query the seeded wiki
make docker-cli ARGS='impact library:redis'   # graph impact
make docker-demo                              # run the latest-phase demo inside the container
```

`make docker-seed` is idempotent — re-running it does not duplicate pages. The
data (pages, sources, `.git`, `state.db`) lives on the `mnesis-data` volume and
survives `docker compose down`; only `docker compose down -v` wipes it.

**Optional profiles** (not started by a plain `up`):
- `docker compose --profile local-llm up -d` — on-host inference (see above); set
  `MNESIS_LLM_PROVIDER=local` in `.env` so sources never leave the box.
- `docker compose --profile maintenance up -d` — periodic decay / graph-lint /
  rebuild upkeep (see above).

### Connect Claude Code

**Networked (deployed HTTP server):** point a client at the HTTP MCP endpoint,
sending the bearer token:

```bash
claude mcp add mnesis --transport http http://<host>:8080/mcp \
  --header "Authorization: Bearer $MNESIS_MCP_TOKEN"
```

**Local (this repo, stdio):** for development you don't need Docker at all — the
repo ships [`.mcp.json`](.mcp.json), so running `claude` from the project root
auto-discovers the server over **stdio** (it spawns `.venv/bin/python -m
mnesis.mcp_server`; run `make setup` first). stdio = local subprocess, no port,
no token; HTTP = networked, token-guarded. Same tools either way.

## Verify the PoC

Run top to bottom on a fresh clone; each step states what you should see.

1. **Install** — `make setup` completes without error and the `mnesis` command
   is available (`uv run mnesis --help` lists the subcommands).
2. **Tests pass** — `make test` reports all tests passing, fully offline.
3. **The loop compounds** — `make demo` prints six steps. Confirm:
   - **Step 2** shows `redactions: 1` and the saved source on disk reads
     `... the API key [REDACTED:SECRET], which must be rotated quarterly.` —
     the fake secret was filtered *before* anything was written.
   - **Step 6** (`query "caching"` after filing the answer back) returns the new
     **digest** page *alongside* the original fact — knowledge that did not exist
     as a page before now surfaces. That is the compounding behaviour.
4. **Try the loop yourself via the CLI** (offline, in a throwaway location so
   your clone stays clean):

   ```bash
   rm -rf /tmp/mnesis-try && git init -q /tmp/mnesis-try
   export MNESIS_LLM_STUB=1 MNESIS_ROOT=/tmp/mnesis-try/wiki
   echo "Project Atlas uses Redis for caching." | uv run mnesis ingest - --ref atlas
   uv run mnesis rebuild
   uv run mnesis query "redis"          # -> the ingested page is the top hit
   uv run mnesis file-back "What caches Atlas?" "Atlas uses Redis for caching." --score 0.9
   uv run mnesis query "caching"        # -> BOTH the fact and the new digest appear
   unset MNESIS_ROOT MNESIS_LLM_STUB
   ```

5. **Canonical-vs-cache holds** — the index is a pure projection of Markdown:

   ```bash
   rm -f /tmp/mnesis-try/wiki/.index/wiki.db && uv run env MNESIS_ROOT=/tmp/mnesis-try/wiki mnesis rebuild
   ```

   deleting the index and rebuilding reproduces identical search results (this
   is also asserted by the test suite).

If every item holds, the Phase-1 PoC is working as designed.

## Verify Phase 2 (confidence & lifecycle)

Phase 2 adds confidence, decay, supersession, and the contradiction review queue.

1. **Lifecycle demo** — `make demo-phase2` (i.e. `uv run python scripts/demo_phase2.py`)
   prints the full lifecycle in six steps. Confirm:
   - **Step 2** — an agreeing source *reinforces* page A: `source_count` becomes
     2 and confidence rises, with **still one page** (no duplicate).
   - **Step 3** — an updating source creates page B that *supersedes* A; A goes
     **stale**.
   - **Step 4** — `query "redis caching"` returns B by default; A reappears
     **demoted** only with `include_stale`.
   - **Step 5** — a low-margin conflicting source is **queued**; `mnesis resolve`
     keeps B, supersedes the conflicter, and empties the queue.
   - **Step 6** — `mnesis decay` transitions an aged, unread page to **stale**.
2. **Confidence & status are surfaced** — `make demo-phase2` output (and
   `mnesis query` / `mnesis get`) show a rounded confidence and status on every
   page; stale pages are marked.
3. **Durable state survives a cache rebuild** — deleting the search index
   (`wiki/.index/wiki.db`) and running `mnesis rebuild` reproduces ranking and
   confidences **without** clearing the durable state store
   (`wiki/.index/state.db`: access counts + review queue). Asserted by
   `tests/test_phase2_e2e.py`.

## Verify Phase 3 (knowledge graph)

Phase 3 extracts entities/relations and projects them into a typed graph.

1. **Graph demo** — `make demo-phase3` (i.e. `uv run python scripts/demo_phase3.py`)
   prints the full walkthrough. Confirm:
   - **Step 2** — `mnesis rebuild` reports the graph it built (entities/edges)
     and the active backend (`sqlite`); `graph-stats` shows counts by type.
   - **Step 3** — `impact library:redis` returns **auth-migration (hop 1)** and
     **Atlas (hop 2)** with the connecting path `project:atlas -> decision:auth-migration
     -> library:redis` — a Redis dependency the Atlas page never states in words.
   - **Step 4** — after a superseding source moves the migration to Postgres, the
     old Redis edge is **demoted** (Redis impact becomes empty) and the new
     Postgres edge **takes over** the chain.
   - **Step 5** — `graph-lint --fix` reports **clean**.
2. **Graph is a rebuildable cache** — deleting both `wiki/.index/wiki.db` and
   `wiki/.index/graph.db` and running `mnesis rebuild` reproduces the graph, the
   search ranking, and confidences, while the durable state store
   (`wiki/.index/state.db`) is preserved. Asserted by `tests/test_phase3_e2e.py`.
3. **Pluggable backend** — `MNESIS_GRAPH_BACKEND` selects the engine (default
   `sqlite`); all graph access goes through one `GraphBackend` interface, so a
   Tier-B backend is a config change, not a refactor.

See [`CLAUDE.md` §13](CLAUDE.md) for the scope map: Phases 1–3 are in scope and
implemented; Phases 4–6 are deferred, with each capability mapped to its phase.
