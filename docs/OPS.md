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

## Maintenance & local-model profiles

- `docker compose --profile maintenance up -d` — periodic `decay` /
  `graph-lint --fix` / rebuild-if-missing against the shared volume (committed,
  git-audited). Tune cadence with `MNESIS_MAINT_INTERVAL`.
- `docker compose --profile local-llm up -d` — on-host inference (Ollama) so
  sources never leave the box; set `MNESIS_LLM_PROVIDER=local` in `.env`.

Neither is started by a plain `docker compose up`.
