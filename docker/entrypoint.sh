#!/bin/sh
# mnesis container entrypoint: prepare the git-backed wiki root, warm the
# rebuildable caches, then dispatch.
set -e

CMD="${1:-serve}"

# --- wiki prep (server / cli / maintenance only) ----------------------------
# The agent runtimes are stateless MCP CLIENTS — no local store, so they skip all
# of this (no /data/mnesis git repo, no rebuild). Both the A-series `agent` and
# the LangGraph `agents` runtime are clients.
if [ "$CMD" != "agent" ] && [ "$CMD" != "agents" ]; then
    ROOT="${MNESIS_ROOT:-/data/mnesis}"

    # ensure the wiki tree and the canonical git repo exist
    mkdir -p "$ROOT/pages" "$ROOT/sources" "$ROOT/.index"

    if [ ! -d "$ROOT/.git" ]; then
        # `|| true`: tolerate a concurrent init race (server + maintenance sidecar
        # starting together) — whichever wins creates a valid repo; the loser's
        # template-hook copy may error harmlessly.
        git init -q "$ROOT" 2>/dev/null || true
    fi
    # A usable local identity so commits never fail (set unconditionally + idempotent,
    # so it is correct even when another container created the repo first).
    git -C "$ROOT" config user.name "mnesis" 2>/dev/null || true
    git -C "$ROOT" config user.email "mnesis@localhost" 2>/dev/null || true

    # warm the rebuildable caches if missing (never touches the durable state)
    if [ ! -f "$ROOT/.index/wiki.db" ] || [ ! -f "$ROOT/.index/graph.db" ]; then
        mnesis rebuild >/dev/null 2>&1 || true
    fi
fi

# --- dispatch ---------------------------------------------------------------
case "$CMD" in
    serve)
        # Launch the MCP server (stdio transport; HTTP is wired in compose).
        exec python -m mnesis.mcp_server
        ;;
    cli)
        shift
        exec mnesis "$@"
        ;;
    maintenance)
        # Periodic upkeep sidecar (decay / graph-lint / rebuild-if-missing).
        exec /usr/local/bin/maintenance.sh
        ;;
    agent)
        # A-series runtime agent — reaches Mnesis only over the MCP endpoint
        # (no volume, no local store). e.g. `agent ingest-daemon --watch /watch`.
        shift
        exec mnesis-agent "$@"
        ;;
    agents)
        # LangGraph agentic runtime — also an MCP-only client. e.g. `agents run`
        # (idle/healthy with no agents registered yet).
        shift
        exec mnesis-agents "$@"
        ;;
    *)
        # Run any given command verbatim (e.g. `id`, `sh`, `mnesis ...`).
        exec "$@"
        ;;
esac
