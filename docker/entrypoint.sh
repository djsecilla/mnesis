#!/bin/sh
# mnesis container entrypoint: prepare the git-backed wiki root, warm the
# rebuildable caches, then dispatch.
set -e

CMD="${1:-serve}"

# --- wiki prep (server / cli / maintenance only) ----------------------------
# The agent is a stateless MCP CLIENT — it has no local store, so it skips all
# of this (no /data/mnesis git repo, no rebuild).
if [ "$CMD" != "agent" ]; then
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
        # Runtime agent — reaches Mnesis only over the MCP endpoint (no volume,
        # no local store). e.g. `agent ingest-daemon --watch /watch`.
        shift
        exec mnesis-agent "$@"
        ;;
    *)
        # Run any given command verbatim (e.g. `id`, `sh`, `mnesis ...`).
        exec "$@"
        ;;
esac
