#!/bin/sh
# mnesis container entrypoint: prepare the git-backed wiki root, warm the
# rebuildable caches, then dispatch.
set -e

# --- env bridge -------------------------------------------------------------
# The image documents the codename names (MNESIS_*); the package reads WIKI_*.
# Map them without overriding an explicitly-set WIKI_* value.
export WIKI_ROOT="${WIKI_ROOT:-${MNESIS_ROOT:-/data/mnesis}}"
[ -n "${MNESIS_LLM_STUB:-}" ] && export WIKI_LLM_STUB="${WIKI_LLM_STUB:-$MNESIS_LLM_STUB}"
[ -n "${MNESIS_GRAPH_BACKEND:-}" ] && export WIKI_GRAPH_BACKEND="${WIKI_GRAPH_BACKEND:-$MNESIS_GRAPH_BACKEND}"

ROOT="$WIKI_ROOT"

# --- ensure the wiki tree and the canonical git repo exist ------------------
mkdir -p "$ROOT/pages" "$ROOT/sources" "$ROOT/.index"

if [ ! -d "$ROOT/.git" ]; then
    git init -q "$ROOT"
    # A usable local identity so commits never fail (overridable via git env).
    git -C "$ROOT" config user.name "mnesis"
    git -C "$ROOT" config user.email "mnesis@localhost"
fi

# --- warm the rebuildable caches (never touches the durable state.db) -------
if [ ! -f "$ROOT/.index/wiki.db" ] || [ ! -f "$ROOT/.index/graph.db" ]; then
    mnesis rebuild >/dev/null 2>&1 || true
fi

# --- dispatch ---------------------------------------------------------------
case "${1:-serve}" in
    serve)
        # Launch the MCP server (stdio transport; HTTP is wired in compose).
        exec python -m mnesis.mcp_server
        ;;
    cli)
        shift
        exec mnesis "$@"
        ;;
    *)
        # Run any given command verbatim (e.g. `id`, `sh`, `mnesis ...`).
        exec "$@"
        ;;
esac
