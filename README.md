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
(`wiki_file_back`) writes a `digest` page — that is the compounding step.

## Prerequisites

- **Python 3.11+** with a `sqlite3` compiled with **FTS5** (the uv-managed and
  python.org CPython builds have it; `make test` will tell you clearly if not).
- **[uv](https://docs.astral.sh/uv/)** for environment and dependency management.
- **git** (the canonical store commits every page mutation).
- *Optional:* `ANTHROPIC_API_KEY` for real LLM extraction. Without it (or with
  `WIKI_LLM_STUB=1`) mnesis runs fully offline with a deterministic stub, so the
  tests and demo never touch the network.

## Setup

```bash
make setup        # uv venv && uv pip install -e .
make test         # full suite, offline (expect: all passing)
make demo         # end-to-end compounding-loop demo, offline
```

`make help` lists every target (`setup`, `test`, `demo`, `run-mcp`, `rebuild`).

By default the wiki lives under `./wiki` (override with `WIKI_ROOT`). The SQLite
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
#    step). Files only if the quality score clears WIKI_FILEBACK_THRESHOLD (0.7).
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

## Connect the MCP server to Claude Code

mnesis exposes its tools over the [Model Context Protocol](https://modelcontextprotocol.io):
`wiki_ingest`, `wiki_query`, `wiki_get`, `wiki_file_back`, `wiki_list`,
`wiki_rebuild`, `wiki_decay`, `wiki_review`, `wiki_resolve`.

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

Set `ANTHROPIC_API_KEY` for real extraction, or `WIKI_LLM_STUB=1` to run offline.

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
   export WIKI_LLM_STUB=1 WIKI_ROOT=/tmp/mnesis-try/wiki
   echo "Project Atlas uses Redis for caching." | uv run mnesis ingest - --ref atlas
   uv run mnesis rebuild
   uv run mnesis query "redis"          # -> the ingested page is the top hit
   uv run mnesis file-back "What caches Atlas?" "Atlas uses Redis for caching." --score 0.9
   uv run mnesis query "caching"        # -> BOTH the fact and the new digest appear
   unset WIKI_ROOT WIKI_LLM_STUB
   ```

5. **Canonical-vs-cache holds** — the index is a pure projection of Markdown:

   ```bash
   rm -f /tmp/mnesis-try/wiki/.index/wiki.db && uv run env WIKI_ROOT=/tmp/mnesis-try/wiki mnesis rebuild
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

See [`CLAUDE.md` §13](CLAUDE.md) for the scope map: Phases 1–2 are in scope and
implemented; Phases 3–6 are deferred, with each capability mapped to its phase.
