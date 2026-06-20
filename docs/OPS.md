# mnesis — Operations Runbook

Operating the containerized core stack (see [`docker-compose.yml`](../docker-compose.yml)
and the README "Run with Docker" section).

## Durable vs. regenerable

The deployment model has a clear split. **Back up the durable layer; the
regenerable layer is rebuilt from it.**

| Path (under `MNESIS_ROOT`, i.e. `/data/mnesis`) | Role | Durability |
|---|---|---|
| `pages/`, `sources/` + `.git/` | Canonical Markdown + full git history (the audit trail) | **Durable — irreplaceable.** Back up. |
| `.index/state.db` | Durable state store: access events + contradiction review queue | **Durable — not derivable from Markdown.** Back up. |
| `.index/wiki.db` | FTS5 search index | Regenerable cache — `mnesis rebuild`. |
| `.index/graph.db` | Knowledge graph | Regenerable cache — `mnesis rebuild`. |
| `.index/*.db-wal`, `*.db-shm` | SQLite WAL sidecars | Transient. Ignore. |

Rule of thumb: **everything in `.index/` except `state.db` is a cache** that
`mnesis rebuild` reconstructs from Markdown (+ `state.db`). Confidence and the
graph degrade gracefully to their Markdown-only values if `state.db` is lost.

**The agent runtime is stateless w.r.t. knowledge.** The `--profile agents`
service holds **no canonical state** — all knowledge lives in `mnesis`. Its
volumes (`mnesis-agents-runs` / `mnesis-agents-state`) carry only agent artefacts:
the append-only run audit, the
LangGraph checkpoints (resumable threads), the dream-cycle **proposals queue +
reports**, the notes-inbox writer's **connector ledger + dead-letter** (under
`/data/agents_runs/connectors`), and the action agent's **action proposals**.
None of it is canonical: losing it is survivable (the next dream cycle re-derives
its proposals; reports are a log; the connector ledger only de-duplicates
already-ingested notes; dead-lettered files can be re-dropped; an action proposal
can simply be re-composed), so it is **not** part of the must-back-up layer above.
Knowledge changes the agents make still go through `mnesis` and are captured in
its git history. (The notes **inbox** is a read-only *input*, and the action
**outbox** holds *output drafts* a human reviews/sends by hand — back up the
source notes and any drafts you care about wherever you keep them, not on the
runtime.)

## Backup

A backup is the **git bundle of the canonical layer** plus a **copy of
`state.db`**. It must NOT depend on `.index/` being present.

```bash
SVC=mnesis        # compose service name
TS=$(date -u +%Y%m%dT%H%M%SZ)

# 1. Git bundle (all branches/history of pages + sources).
docker compose exec -T $SVC git -C /data/mnesis bundle create /tmp/mnesis-$TS.bundle --all
docker compose cp $SVC:/tmp/mnesis-$TS.bundle ./mnesis-$TS.bundle

# 2. Durable state store (skip if it does not exist yet).
docker compose exec -T $SVC sh -c '[ -f /data/mnesis/.index/state.db ] && cp /data/mnesis/.index/state.db /tmp/state.db || echo "no state.db yet"'
docker compose cp $SVC:/tmp/state.db ./state-$TS.db 2>/dev/null || true
```

Store `mnesis-$TS.bundle` + `state-$TS.db` off-box. **Do not** back up `.index/`
(`wiki.db`/`graph.db`) — they are regenerated on restore.

## Restore

Restore the canonical layer from the bundle and `state.db`, then **rebuild** the
caches. Into a fresh volume:

```bash
docker compose down                              # stop (volume kept)
docker volume rm mnesis_mnesis-data || true      # start from empty (or use a new project)

# Bring up so the volume + git repo exist, then restore into it.
docker compose up -d $SVC
docker compose cp ./mnesis-<TS>.bundle $SVC:/tmp/restore.bundle
docker compose cp ./state-<TS>.db      $SVC:/tmp/state.db

docker compose exec -T $SVC sh -c '
  set -e
  cd /data/mnesis
  git fetch /tmp/restore.bundle "refs/heads/*:refs/heads/*" || git pull /tmp/restore.bundle
  git checkout -f -B master FETCH_HEAD 2>/dev/null || git checkout -f master
  mkdir -p .index && cp /tmp/state.db .index/state.db
  mnesis rebuild           # regenerates wiki.db + graph.db from Markdown + state.db
'
```

(For a clone-from-scratch you can also `git clone /tmp/restore.bundle /data/mnesis`
into an empty directory, then drop in `state.db` and `mnesis rebuild`.)

After restore, `mnesis query`/`mnesis impact` return the same results as before —
the search ranking and graph are deterministic projections of the canonical
layer (+ `state.db`).

## Cache-only recovery

If only the caches are corrupt/missing (`.index/wiki.db` or `graph.db`), no
restore is needed — just rebuild (keeps `state.db`):

```bash
docker compose exec -T mnesis sh -c 'rm -f /data/mnesis/.index/wiki.db /data/mnesis/.index/graph.db; mnesis rebuild'
```

## Health checks

- **Container**: `docker compose ps` shows `mnesis` **and** `mnesis-ui` as
  `healthy` (mnesis probes `GET /health`; the UI probes its served index).
- **HTTP** (unauthenticated, safe to probe): `curl http://<host>:8080/health` →
  `{"status":"ok","pages":N,"index_present":true,"graph_present":true}`.
- **UI through the proxy**: `curl http://<host>:3000/` returns the app shell;
  `curl http://<host>:3000/api/pages` returns JSON (nginx adds the bearer token
  server-side, so no `Authorization` header is needed from the client).
- **Logs**: `make docker-logs` (tails `mnesis` + `mnesis-ui`).
- **Graph consistency**: `make docker-cli ARGS="graph-lint"` (report-only) or
  `ARGS="graph-lint --fix"`.

## Web UI (`mnesis-ui`)

The `mnesis-ui` service is a static nginx container: it serves the SPA and
reverse-proxies `/api` (+ the SSE chat stream, with buffering off) to `mnesis`
on the internal network. It is **stateless** — no volume, nothing to back up —
so it can be rebuilt or removed at any time with zero data loss (`docker compose
up -d --build mnesis-ui`, or `docker compose rm -sf mnesis-ui`); all state lives
in mnesis. Ports: host `${MNESIS_UI_PORT:-3000}` → container `80`. **Token
model:** when `MNESIS_MCP_TOKEN` is set it is injected into proxied `/api`
requests server-side, so the browser never holds it; the host/network is the
trust boundary (per-user auth is a future iteration). Rotating the token only
needs the UI recreated alongside mnesis (`docker compose up -d --force-recreate
mnesis mnesis-ui`) so nginx re-reads the new value.

## Rotating `MNESIS_MCP_TOKEN`

The bearer token is supplied only via env (`.env`), never baked into the image.
To rotate:

```bash
# 1. Set the new token in .env (MNESIS_MCP_TOKEN=...).
$EDITOR .env
# 2. Recreate the service so it picks up the new env (volume/data untouched).
docker compose up -d --force-recreate mnesis
# 3. Update every MCP client's Authorization header to the new token.
```

There is no token stored server-side beyond the env var, so rotation is just an
env change + recreate. Health (`/health`) stays unauthenticated throughout.

## Maintenance (the dream-cycle agent) & local inference

- **Periodic maintenance is owned by the scheduled dream-cycle agent** in the
  `mnesis-agents-runtime` service: `docker compose --profile agents up -d`. Each
  cycle runs over MCP — auto-applying safe hygiene (`decay` + safe graph-lint
  fixes, committed/git-audited server-side) and surfacing contradiction/dedup
  **proposals** for human review (it never auto-resolves or auto-merges). Cadence:
  `MNESIS_AGENTS_DREAM_INTERVAL_SECONDS` (default ~daily; a precise cron
  `MNESIS_AGENTS_DREAM_CRON` needs the APScheduler extra). On demand:
  `docker compose run --rm mnesis-agents-runtime agents dream-cycle --now`;
  latest report via `… --report`. Proposals + reports persist on the
  `mnesis-agents-runs` volume (`proposals.jsonl`, `dream-cycles.jsonl`).
- **The old `--profile maintenance` sidecar is RETIRED.** It is no longer in
  `docker-compose.yml`; periodic upkeep now has exactly **one** scheduler (the
  agent), so there is no double-run. `docker compose run --rm mnesis cli decay`
  remains a manual one-off (not a scheduler) if you need it.
- **Local inference** (sources never leave the box): run your own Ollama (or any
  OpenAI-compatible server) on the **host** and set `MNESIS_LLM_PROVIDER=local`
  in `.env` (mnesis reaches it at `MNESIS_LLM_BASE_URL`, default
  `http://host.docker.internal:11434`), then
  `docker compose up -d --force-recreate mnesis`. No Ollama container is run.

## External send (email) — staged rollout

The action agent can send an **email** (the one external channel). It is
**default-off**: with the shipped config, `docker compose --profile agents up -d`
runs with **no egress at all** — the channel isn't registered, the egress plane is
disabled, and email is dry-run. Enable a real send only through the staged sequence
below; each stage is one reviewed `.env` change + a runtime recreate
(`docker compose up -d --force-recreate mnesis-agents-runtime`).

**Secrets.** `MNESIS_SMTP_PASSWORD` (and any credential) lives **only in `.env`**
(gitignored) or your secret store, loaded via `env_file`. It is **never** written
into `docker-compose.yml` or baked into the image, and it never appears in any log,
proposal, draft, or send-audit record. Rotate it by editing `.env` and recreating
the runtime.

**Durability.** The egress quota ledger (`egress.json`) and the immutable,
hash-chained **send-audit** (`send_audit.jsonl`) persist under
`/data/agents_runs/connectors` on the `mnesis-agents-runs` volume — one record per
send attempt (ids, recipient, endpoint, content hash, decision, status; never the
body or a secret). Verify the chain at any time with
`docker compose run --rm --entrypoint python mnesis-agents-runtime -c
'from mnesis_agents.send_audit import SendAuditLog; print(SendAuditLog().verify())'`
→ `(True, None)`.

### Stage 1 — dry-run only (renders, sends nothing)

```ini
# .env
MNESIS_EMAIL_ENABLED=1        # register the email channel (still cannot send)
# MNESIS_EGRESS_ENABLED stays unset → default-deny
# MNESIS_EMAIL_DRYRUN defaults to 1 → render only
```

```bash
docker compose up -d --force-recreate mnesis-agents-runtime
docker compose run --rm mnesis-agents-runtime agents action prepare-meeting-brief \
  --context '{"topic":"Atlas caching","recipient":"you@example.com"}' --channel email
docker compose run --rm mnesis-agents-runtime agents actions show <id>      # dry-run preview
docker compose run --rm mnesis-agents-runtime agents actions approve <id> \
  --confirm-recipient you@example.com                                       # → status dry_run; nothing sent
```

Confirm the rendered subject/body/recipient are exactly right before going further.

### Stage 2 — self-send drill (one real email, to yourself)

Allowlist **only your own verified address**, point at your SMTP endpoint, put the
password in `.env`, then turn dry-run off:

```ini
# .env
MNESIS_EMAIL_ENABLED=1
MNESIS_EGRESS_ENABLED=1
MNESIS_EGRESS_RECIPIENT_ALLOWLIST=you@example.com     # ONLY your own address
MNESIS_EGRESS_ENDPOINT_ALLOWLIST=smtp.example.com:587
MNESIS_EMAIL_FROM=you@example.com
MNESIS_SMTP_HOST=smtp.example.com
MNESIS_SMTP_PORT=587
MNESIS_SMTP_USERNAME=you@example.com
MNESIS_SMTP_PASSWORD=__from_your_secret_store__       # in .env only, never compose/image
MNESIS_EMAIL_STARTTLS=1
MNESIS_EMAIL_DRYRUN=0                                  # the live switch — set LAST
```

```bash
docker compose up -d --force-recreate mnesis-agents-runtime
# Propose → confirm the recipient is yourself → approve:
docker compose run --rm mnesis-agents-runtime agents action prepare-meeting-brief \
  --context '{"topic":"Self-send test","recipient":"you@example.com"}' --channel email
docker compose run --rm mnesis-agents-runtime agents actions approve <id> \
  --confirm-recipient you@example.com                 # → status sent (exactly once)
```

Verify: the email arrives in your inbox; the send-audit has one new `sent` record
for your address and `verify()` is `(True, None)`. A second approval of the same
proposal does **not** re-send (at-most-once). Any recipient *not* on the allowlist
is refused even now.

### Stage 3 — add further recipients, one at a time

Append each additional **verified** address to `MNESIS_EGRESS_RECIPIENT_ALLOWLIST`
individually, recreate, and test a single send to it before adding the next. Keep
the list as small as the use case allows.

### Kill-switch & rollback

Set `MNESIS_EGRESS_KILL=1` (and recreate) to deny **all** egress immediately — it is
re-checked at the last moment before transmit, so it halts even an already-approved
send. To stand down entirely, unset `MNESIS_EGRESS_ENABLED` (or `MNESIS_EMAIL_ENABLED`)
and recreate; the channel falls back to dry-run / unregistered. Quotas
(`MNESIS_EGRESS_*_QUOTA` / `_RATE_LIMIT`; `0` = deny all) cap volume per recipient
and globally — an over-quota send is `blocked` and audited.
