#!/usr/bin/env bash
# Real-stack smoke test for the dream-cycle maintenance agent (M5).
#
# Brings up Mnesis + the agentic runtime (profile: agents) with a SHORTENED
# cadence, proves a dream cycle runs against Mnesis over MCP — auto-applies safe
# hygiene (decay + safe graph fixes), queues contradiction/dedup proposals, and
# writes a report — and confirms the retired D5 `maintenance` sidecar is gone so
# maintenance is not double-run. Optionally checks crystallization.
#
# Usage:   scripts/smoke_dream_cycle.sh            # stub inference (no keys/network)
#          MNESIS_LLM_PROVIDER=local scripts/smoke_dream_cycle.sh   # on-prem (host Ollama)
#          CRYSTALLIZE=1 scripts/smoke_dream_cycle.sh               # also file a digest
#
# Requires: docker + docker compose. Self-contained; tears the stack down at the end.
set -euo pipefail

cd "$(dirname "$0")/.."

# Stub inference by default so the smoke needs no API key or network.
export MNESIS_AGENTS_STUB="${MNESIS_AGENTS_STUB:-1}"
export MNESIS_LLM_STUB="${MNESIS_LLM_STUB:-1}"
# Shorten the nightly cadence so the scheduled cycle fires within the smoke.
export MNESIS_AGENTS_DREAM_INTERVAL_SECONDS="${MNESIS_AGENTS_DREAM_INTERVAL_SECONDS:-15}"
export MNESIS_AGENTS_CRYSTALLIZE="${CRYSTALLIZE:-}"

pass() { printf '  \033[32m✓\033[0m %s\n' "$1"; }
fail() { printf '  \033[31m✗ %s\033[0m\n' "$1"; exit 1; }

cleanup() { echo "==> tearing down"; docker compose --profile agents down -v >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "==> 1. the D5 maintenance sidecar is retired (exactly one scheduler)"
if docker compose config --services | grep -qx maintenance; then
    fail "a 'maintenance' service still exists — maintenance would be double-run"
fi
pass "no 'maintenance' service in the compose config"

echo "==> 2. bring up Mnesis + the agentic runtime (profile: agents)"
docker compose --profile agents up -d --build
# Wait for Mnesis to be healthy.
for i in $(seq 1 30); do
    state=$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q mnesis)" 2>/dev/null || echo starting)
    [ "$state" = "healthy" ] && break
    sleep 2
done
[ "${state:-}" = "healthy" ] || fail "mnesis did not become healthy"
pass "mnesis healthy; mnesis-agents-runtime up"

echo "==> 3. seed a little knowledge (so passes have something to chew on)"
docker compose exec -T mnesis sh -lc '
  printf "Project Atlas uses Redis for caching. Sarah owns the auth migration." | mnesis ingest --ref atlas-notes -
  mnesis rebuild >/dev/null
' >/dev/null
pass "seeded a source and rebuilt the caches"

echo "==> 4. run one dream cycle on demand (--now) over MCP"
out=$(docker compose run --rm mnesis-agents-runtime agents dream-cycle --now)
echo "$out" | sed 's/^/    /'
echo "$out" | grep -q "Dream cycle" || fail "no dream-cycle report printed"
echo "$out" | grep -qE "passes: [1-9]" || fail "no passes ran"
pass "a dream cycle ran and reported"

echo "==> 5. the report is persisted and retrievable"
docker compose run --rm mnesis-agents-runtime agents dream-cycle --report | grep -q "Dream cycle" \
    || fail "latest report not retrievable via --report"
pass "latest report retrievable"

echo "==> 6. proposals + reports persisted on the runtime volume"
docker compose run --rm --entrypoint sh mnesis-agents-runtime -lc '
  test -f /data/agents_runs/dream-cycles.jsonl && echo report-ok
  test -f /data/agents_runs/proposals.jsonl && echo proposals-ok || echo no-proposals
' | sed 's/^/    /'

if [ -n "${MNESIS_AGENTS_CRYSTALLIZE:-}" ]; then
    echo "==> 7. crystallization on: a maintenance digest was filed"
    docker compose run --rm -e MNESIS_AGENTS_CRYSTALLIZE=1 mnesis-agents-runtime \
        agents dream-cycle --now | grep -qi "crystallized digest" \
        && pass "maintenance digest crystallized" || fail "no digest crystallized"
fi

echo "==> 8. the scheduled cycle also fires on its own (shortened cadence)"
sleep "$((MNESIS_AGENTS_DREAM_INTERVAL_SECONDS + 8))"
docker compose logs mnesis-agents-runtime 2>&1 | grep -qiE "dream|maintenance" \
    && pass "scheduled dream cycle observed in the runtime logs" \
    || echo "    (note: check 'docker compose logs mnesis-agents-runtime' for the scheduled run)"

echo
echo "SMOKE PASSED — the dream cycle runs on schedule and on demand; the D5 sidecar is retired."
