#!/bin/sh
# mnesis container entrypoint: prepare the git-backed wiki root, warm the
# rebuildable caches, then dispatch.
set -e

ROOT="${MNESIS_ROOT:-/data/mnesis}"

# --- ensure the wiki tree and the canonical git repo exist ------------------
mkdir -p "$ROOT/pages" "$ROOT/sources" "$ROOT/.index"

if [ ! -d "$ROOT/.git" ]; then
    git init -q "$ROOT"
    # A usable local identity so commits never fail (overridable via git env).
    git -C "$ROOT" config user.name "mnesis"
    git -C "$ROOT" config user.email "mnesis@localhost"
fi

# --- warm the rebuildable caches if missing (never touches the durable state) -
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
