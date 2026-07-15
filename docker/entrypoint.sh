#!/bin/sh
# mnesis container entrypoint: prepare the git-backed wiki root, warm the
# rebuildable caches, then dispatch.
set -e

CMD="${1:-serve}"

# --- wiki prep (server / cli / maintenance only) ----------------------------
# The `agents` runtime is a stateless MCP CLIENT — no local store, so it skips all
# of this (no /data/mnesis git repo, no rebuild).
if [ "$CMD" != "agents" ]; then
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
# --- first-run auth bootstrap (IAM8) ----------------------------------------
# Real auth needs a real login. On the `serve` path we (idempotently, guarded)
# bootstrap the identities from operator-supplied passwords — NEVER a default:
#   - MNESIS_BOOTSTRAP_PASSWORD -> the system-admin (tenant lifecycle root of trust)
#   - MNESIS_WEB_ADMIN_PASSWORD -> the first tenant-admin web user (default tenant)
# Both are no-ops if that admin already exists, so restarts are safe.
if [ "$CMD" = "serve" ]; then
    if [ -n "${MNESIS_BOOTSTRAP_PASSWORD:-}" ]; then
        mnesis admin bootstrap --password "$MNESIS_BOOTSTRAP_PASSWORD" >/dev/null 2>&1 || true
    fi
    # Initial admin (R2): role=admin + its tenant + a default vault, in the
    # must_change_password state. Config-driven (MNESIS_ADMIN_* preferred; the legacy
    # MNESIS_WEB_ADMIN_* still work). Guarded/idempotent/no-clobber; no default password.
    ADMIN_PW="${MNESIS_ADMIN_PASSWORD:-${MNESIS_WEB_ADMIN_PASSWORD:-}}"
    if [ -n "$ADMIN_PW" ]; then
        mnesis --tenant "${MNESIS_ADMIN_TENANT:-${MNESIS_WEB_ADMIN_TENANT:-default}}" init-admin \
            --principal "${MNESIS_ADMIN_USERNAME:-${MNESIS_WEB_ADMIN_USER:-admin}}" \
            --password "$ADMIN_PW" || true
    fi
fi

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
        # Manual upkeep loop (decay / graph-lint / rebuild-if-missing). The
        # scheduled `maintenance` COMPOSE SIDECAR is RETIRED — periodic upkeep is
        # now owned solely by the dream-cycle agent (profile: agents). This case
        # remains only as a manual escape hatch (`docker compose run --rm mnesis
        # maintenance`); do not run it as a service alongside the agents profile.
        exec /usr/local/bin/maintenance.sh
        ;;
    agents)
        # LangGraph agentic runtime — an MCP-only client (no volume, no local
        # store). e.g. `agents run` (dream cycle + notes inbox + action agent).
        shift
        exec mnesis-agents "$@"
        ;;
    *)
        # Run any given command verbatim (e.g. `id`, `sh`, `mnesis ...`).
        exec "$@"
        ;;
esac
