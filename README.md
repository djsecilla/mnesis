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

Two utilities round it out: `mnesis list` (all pages) and `mnesis rebuild`
(reconstruct the search index from Markdown). Run any command under
`uv run mnesis ...` if you have not activated the venv.

## Connect the MCP server to Claude Code

mnesis exposes its tools over the [Model Context Protocol](https://modelcontextprotocol.io):
`wiki_ingest`, `wiki_query`, `wiki_get`, `wiki_file_back`, `wiki_list`,
`wiki_rebuild`.

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
   is available (`uv run mnesis --help` lists the six subcommands).
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

If every item holds, the Phase-1 PoC is working as designed. See
[`CLAUDE.md` §13](CLAUDE.md) for what is deliberately **out of scope** here and
which later phase each deferred capability lands in.
