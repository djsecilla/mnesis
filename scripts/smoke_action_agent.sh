#!/usr/bin/env bash
# Real-stack smoke test for the approval-gated, draft-only action agent (A5).
#
# Brings up Mnesis + the agentic runtime (profile: agents) and proves the action
# flow end to end — draft-only by default, gated, no external egress here:
#   - seed a little knowledge so a brief is grounded;
#   - `agents action prepare-meeting-brief` → a PENDING proposal, NOTHING delivered;
#   - `agents actions approve <id>` → writes the draft to the outbox volume;
#   - a second proposal `reject`ed → nothing delivered;
#   - confirm the DEFAULT channel registry is inert-only (the email channel is
#     opt-in via MNESIS_EMAIL_ENABLED and not enabled here → no egress possible).
#
# Usage:   scripts/smoke_action_agent.sh
#          MNESIS_LLM_PROVIDER=local scripts/smoke_action_agent.sh   # on-prem
#
# Requires docker + docker compose. Self-contained; tears the stack down at the end.
set -euo pipefail
cd "$(dirname "$0")/.."

export MNESIS_AGENTS_STUB="${MNESIS_AGENTS_STUB:-1}"
export MNESIS_LLM_STUB="${MNESIS_LLM_STUB:-1}"
# Keep the periodic agents quiet; we drive the action agent on demand.
export MNESIS_AGENTS_DREAM_ENABLED="${MNESIS_AGENTS_DREAM_ENABLED:-0}"
export MNESIS_NOTES_ENABLED="${MNESIS_NOTES_ENABLED:-0}"
OUTBOX="./action_outbox"

pass() { printf '  \033[32m✓\033[0m %s\n' "$1"; }
fail() { printf '  \033[31m✗ %s\033[0m\n' "$1"; exit 1; }
cleanup() {
    echo "==> tearing down"
    docker compose --profile agents down -v >/dev/null 2>&1 || true
    rm -f "$OUTBOX"/*.md "$OUTBOX"/notifications.jsonl 2>/dev/null || true
}
trap cleanup EXIT
mkdir -p "$OUTBOX"

echo "==> 1. the default channel registry is inert-only (email opt-in, off here)"
docker compose run --rm --entrypoint python mnesis-agents-runtime -c \
  'from mnesis_agents.channels import default_channel_registry as r; reg=r(); \
   import sys; sys.exit(0 if all(reg.risk_class(n)=="inert" for n in reg.names()) else 1)' \
  && pass "the default channel registry is inert-only (draft-outbox, local-notify)" \
  || fail "a non-inert channel exists"

echo "==> 2. bring up Mnesis + the agentic runtime"
docker compose --profile agents up -d --build
for i in $(seq 1 30); do
    state=$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q mnesis)" 2>/dev/null || echo starting)
    [ "$state" = "healthy" ] && break; sleep 2
done
[ "${state:-}" = "healthy" ] || fail "mnesis did not become healthy"
pass "mnesis healthy; action agent available"

echo "==> 3. seed knowledge so the brief is grounded"
docker compose exec -T mnesis sh -lc '
  printf "Project Atlas uses Redis for caching. Sarah owns the auth migration." | mnesis ingest --ref atlas-notes -
  mnesis rebuild >/dev/null' >/dev/null
pass "seeded a source"

echo "==> 4. on-demand action → a PENDING proposal, NOTHING delivered"
out=$(docker compose run --rm mnesis-agents-runtime agents action prepare-meeting-brief \
        --context '{"topic":"Atlas caching","attendees":["Sarah"]}')
echo "$out" | sed 's/^/    /'
echo "$out" | grep -q "proposed" || fail "no proposal was created"
PID=$(echo "$out" | sed -n 's/.*proposal \([0-9a-f]\{8,\}\).*/\1/p' | head -1)
[ -n "$PID" ] || fail "could not parse the proposal id"
[ -z "$(ls "$OUTBOX"/*.md 2>/dev/null)" ] && pass "proposal $PID pending; outbox empty (nothing delivered)" \
    || fail "a draft was delivered without approval"

echo "==> 5. list pending proposals"
docker compose run --rm mnesis-agents-runtime agents actions | grep -q "$PID" \
    && pass "the proposal is listed as pending" || fail "proposal not listed"

echo "==> 6. approve → the draft is written to the outbox volume"
docker compose run --rm mnesis-agents-runtime agents actions approve "$PID" | sed 's/^/    /'
sleep 1
ls "$OUTBOX"/*.md >/dev/null 2>&1 && pass "draft written to the outbox on approval" \
    || fail "no draft appeared in the outbox after approval"
grep -rqi "redis" "$OUTBOX"/*.md && pass "the draft is grounded (mentions seeded knowledge)" || true

echo "==> 7. a rejected proposal delivers nothing"
out2=$(docker compose run --rm mnesis-agents-runtime agents action prepare-meeting-brief \
         --context '{"topic":"Auth migration"}')
PID2=$(echo "$out2" | sed -n 's/.*proposal \([0-9a-f]\{8,\}\).*/\1/p' | head -1)
before=$(ls "$OUTBOX"/*.md 2>/dev/null | wc -l | tr -d ' ')
docker compose run --rm mnesis-agents-runtime agents actions reject "$PID2" >/dev/null
after=$(ls "$OUTBOX"/*.md 2>/dev/null | wc -l | tr -d ' ')
[ "$before" = "$after" ] && pass "reject delivered nothing (draft count unchanged: $before)" \
    || fail "a draft appeared after a rejection"

echo
echo "SMOKE PASSED — action agent proposes, approval writes a draft, reject discards; only inert channels, no external egress."
