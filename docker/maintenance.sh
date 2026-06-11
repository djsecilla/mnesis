#!/bin/sh
# mnesis maintenance sidecar: run upkeep commands on a cadence against the shared
# volume. Deployment-level scheduling until Phase 4 moves these into the app as
# event hooks. Commands run through the CLI, so every change is committed/audited
# in the volume's git history. Tolerates the server running concurrently (WAL
# reads + brief write contention retried, never crashing the loop).
#
# `set -u` only — never `-e`: a failing command must not kill the loop.
set -u

ROOT="${MNESIS_ROOT:-/data/mnesis}"
INTERVAL="${MNESIS_MAINT_INTERVAL:-86400}"   # default: daily

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

# Is a CLI subcommand available in this image (e.g. graph-lint pre-Phase-3)?
supported() { mnesis --help 2>/dev/null | grep -q -- "$1"; }

# Run a CLI command, retrying brief contention; never aborts the script.
run() {
    out=""
    for attempt in 1 2 3; do
        if out=$(mnesis "$@" 2>&1); then
            printf '%s\n' "$out" | sed 's/^/    /'
            return 0
        fi
        log "    '$*' failed (attempt $attempt) — likely write contention; retrying in 2s"
        sleep 2
    done
    log "    '$*' did not succeed after retries; skipping this cycle"
    printf '%s\n' "$out" | tail -2 | sed 's/^/    /'
    return 1
}

cycle() {
    log "maintenance cycle start (root=$ROOT)"

    # 1. rebuild-if-missing — regenerate the rebuildable caches if absent.
    if [ ! -f "$ROOT/.index/wiki.db" ] || [ ! -f "$ROOT/.index/graph.db" ]; then
        log "  caches missing -> rebuild"
        run rebuild
    else
        log "  caches present -> rebuild skipped"
    fi

    # 2. decay — recompute confidence and age active<->stale.
    if supported decay; then
        log "  decay:"
        run decay
    else
        log "  decay: skipped (not available in this build)"
    fi

    # 3. graph-lint --fix — only if Phase 3 (graph) is present.
    if supported graph-lint; then
        log "  graph-lint --fix:"
        run graph-lint --fix
    else
        log "  graph-lint: skipped (Phase 3 not present)"
    fi

    log "maintenance cycle done"
}

log "maintenance sidecar starting; interval=${INTERVAL}s"
while true; do
    cycle
    sleep "$INTERVAL"
done
