# mnesis (MVP PoC)

mnesis is a knowledge base that **compounds** instead of resetting. Where
retrieval-augmented generation fetches context and forgets it, this wiki
accumulates and reinforces it: sources are filtered, ingested, and written as
canonical Markdown pages; those pages are indexed for keyword search; queries
draw on them; and synthesized answers are filed back as durable, retrievable
knowledge. This repository is the Phase-1 MVP proof of concept demonstrating
that end-to-end loop — *filter → ingest → write → index → query → file back →
query again and see it surface*. The design contract lives in
[`CLAUDE.md`](CLAUDE.md), the authoritative schema document for the system.

## Setup

```bash
# 1. Create a virtual environment (Python 3.11+)
uv venv

# 2. Install the package (editable) and its dependencies
uv pip install -e .

# 3. Run the tests
uv run pytest -q

# 4. Invoke the CLI
uv run mnesis
```

By default the wiki lives under `./wiki` (override with `WIKI_ROOT`). The
SQLite search index under `wiki/.index/` is a rebuildable cache and is not
tracked by git — Markdown is the single source of truth.

## MCP server

mnesis exposes its tools over the [Model Context Protocol](https://modelcontextprotocol.io)
so Claude Code and other agents can use the wiki natively. Tools:
`wiki_ingest`, `wiki_query`, `wiki_get`, `wiki_file_back`, `wiki_list`,
`wiki_rebuild`.

**Run the server standalone** (stdio transport):

```bash
uv run python -m mnesis.mcp_server
```

**Claude Code auto-discovery.** This repo ships a [`.mcp.json`](.mcp.json) that
registers the server, so running `claude` from the project root discovers it
automatically (approve the project-scoped server when prompted). It launches
`.venv/bin/python -m mnesis.mcp_server`, so make sure you have run
`uv venv && uv pip install -e .` first.

**`claude mcp add` alternative.** To register it yourself instead of relying on
`.mcp.json`:

```bash
claude mcp add mnesis -- uv run python -m mnesis.mcp_server
```

Set `ANTHROPIC_API_KEY` for real extraction, or `WIKI_LLM_STUB=1` to run the
server fully offline with the deterministic stub.
