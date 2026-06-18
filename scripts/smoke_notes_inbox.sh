#!/usr/bin/env bash
# Real-stack smoke test for the notes-inbox writing agent (W5).
#
# Brings up Mnesis + the agentic runtime (profile: agents) with a SHORT poll
# interval, then proves the connector→agent pipeline end to end:
#   - drop a .md  -> parsed, ingested into Mnesis (visible via query), secret REDACTED;
#   - re-drop it  -> no duplicate page;
#   - drop a binary "note" -> dead-lettered with a reason (no silent loss).
#
# Usage:   scripts/smoke_notes_inbox.sh
#          MNESIS_LLM_PROVIDER=local scripts/smoke_notes_inbox.sh   # on-prem (host Ollama)
#
# Requires docker + docker compose. Self-contained; tears the stack down at the end.
set -euo pipefail
cd "$(dirname "$0")/.."

export MNESIS_AGENTS_STUB="${MNESIS_AGENTS_STUB:-1}"
export MNESIS_LLM_STUB="${MNESIS_LLM_STUB:-1}"
export MNESIS_NOTES_POLL_INTERVAL="${MNESIS_NOTES_POLL_INTERVAL:-3}"
# Keep maintenance from firing during the smoke (the notes writer is what we test).
export MNESIS_AGENTS_DREAM_ENABLED="${MNESIS_AGENTS_DREAM_ENABLED:-0}"
INBOX="./notes_inbox"

pass() { printf '  \033[32m✓\033[0m %s\n' "$1"; }
fail() { printf '  \033[31m✗ %s\033[0m\n' "$1"; exit 1; }
cleanup() {
    echo "==> tearing down"
    docker compose --profile agents down -v >/dev/null 2>&1 || true
    rm -f "$INBOX"/smoke-*.md "$INBOX"/smoke-bad.md 2>/dev/null || true
}
trap cleanup EXIT

mkdir -p "$INBOX"

echo "==> 1. bring up Mnesis + the agentic runtime (notes inbox)"
docker compose --profile agents up -d --build
for i in $(seq 1 30); do
    state=$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q mnesis)" 2>/dev/null || echo starting)
    [ "$state" = "healthy" ] && break; sleep 2
done
[ "${state:-}" = "healthy" ] || fail "mnesis did not become healthy"
pass "mnesis healthy; mnesis-agents-runtime watching /data/notes_inbox"

echo "==> 2. drop a note containing a secret"
KEY="AKIA1234567890SMOKE0"   # a fake AWS-key-shaped secret the scrubber should redact
cat > "$INBOX/smoke-redis.md" <<EOF
Project Atlas uses Redis for caching. Connect with api_key=$KEY (rotate quarterly).
EOF
sleep "$((MNESIS_NOTES_POLL_INTERVAL + 6))"

echo "==> 3. the note was ingested and is queryable"
docker compose exec -T mnesis mnesis query "redis caching" | grep -qi redis \
    || fail "the note did not become a queryable page"
pass "note ingested and surfaces via query"

echo "==> 4. the secret is REDACTED in the stored knowledge (Mnesis's job)"
PAGES=$(docker compose exec -T mnesis sh -lc 'grep -rl "" /data/mnesis/pages /data/mnesis/sources 2>/dev/null | tr "\n" " "')
if docker compose exec -T mnesis sh -lc "grep -rq '$KEY' /data/mnesis/pages /data/mnesis/sources"; then
    fail "the raw secret leaked into the stored page/source"
fi
docker compose exec -T mnesis sh -lc "grep -rq 'REDACTED' /data/mnesis/sources" \
    && pass "secret redacted in the stored source (no raw value on disk)" \
    || echo "    (note: redaction marker not found — check the scrubber config)"

echo "==> 5. re-drop identical content -> no duplicate"
before=$(docker compose exec -T mnesis sh -lc 'ls /data/mnesis/pages | wc -l' | tr -d ' \r')
cat > "$INBOX/smoke-redis.md" <<EOF
Project Atlas uses Redis for caching. Connect with api_key=$KEY (rotate quarterly).
EOF
sleep "$((MNESIS_NOTES_POLL_INTERVAL + 6))"
after=$(docker compose exec -T mnesis sh -lc 'ls /data/mnesis/pages | wc -l' | tr -d ' \r')
[ "$before" = "$after" ] && pass "re-drop did not create a duplicate page ($before == $after)" \
    || fail "page count changed on identical re-drop ($before -> $after)"

echo "==> 6. a malformed note dead-letters with a reason"
printf '\xff\xfe\x00 not valid utf-8 \xff' > "$INBOX/smoke-bad.md"
sleep "$((MNESIS_NOTES_POLL_INTERVAL + 6))"
docker compose exec -T mnesis-agents-runtime sh -lc \
    'cat /data/agents_runs/connectors/dead-letter.jsonl 2>/dev/null' | grep -q 'smoke-bad.md' \
    && pass "malformed note recorded in the dead-letter (no silent loss)" \
    || fail "malformed note did not dead-letter"

echo
echo "SMOKE PASSED — notes drop in, get ingested (secrets redacted), re-drops dedup, bad files dead-letter."
