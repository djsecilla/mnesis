# mnesis — Docker Deployment Playbook

**Containerize and spin up the whole system with Docker Compose. A sequenced prompt set for Claude Code (Opus 4.6).**

This playbook packages the wiki — the Python app, the MCP server, the canonical git store, and the SQLite/Kùzu indexes — into a Compose deployment you can bring up with one command. It is **orthogonal to the feature phases**: run it after any phase. The prompts adapt to whatever is built; they assume only that the `mnesis` package and its `wiki` CLI exist.

It matches the **current Tier-A architecture**: a single application container with embedded SQLite and Kùzu, and a persistent volume. It is *not* the full Tier-B topology (Postgres + pgvector + AGE, Qdrant, Redis, Temporal) — those services arrive with Phases 5–6, and D3 leaves a clearly-marked seam for them.

---

## Two facts that shape the whole deployment

**1. The MCP server must change transport to be networked.** Locally, Claude Code *spawns* the server over **stdio** — there is nothing to "host." To reach it from a container over a network, the server needs an **HTTP transport** (streamable HTTP / SSE). D2 adds that as an option; stdio stays the default for local use.

**2. The canonical store is a git repo inside a volume.** This dictates the image and the persistence model:

| Path (under the wiki root) | Role | Durability |
|---|---|---|
| `pages/`, `sources/`, `.git/` | Canonical knowledge + audit trail | **Durable — must persist and be backed up.** |
| `.index/state.db` | Access events + contradiction review queue | **Durable — not regenerable from Markdown.** |
| `.index/wiki.db` | FTS5 search index | Regenerable via `wiki rebuild`. |
| `.index/graph` | Kùzu graph | Regenerable via `wiki rebuild`. |

So: `git` must be installed in the image; the volume holds the whole wiki root; backups need only the git tree plus `state.db`; a fresh container can regenerate the rest with `wiki rebuild`.

---

## Reusing the standard template & rules

Same six-part template — **CONTEXT / OBJECTIVE / BUILD / CONSTRAINTS / ACCEPTANCE / ON DONE** — and the standing rules: things run offline with `WIKI_LLM_STUB=1` where inference would otherwise be needed; conventional commits; self-checking acceptance; keep `README` and `CLAUDE.md` in sync. Deployment prompts are prefixed **D**.

---

# The Prompts

---

## Prompt D1 — Containerization: Dockerfile + entrypoint

```
CONTEXT: The mnesis package and the `wiki` CLI exist. Package the application into a container image. Remember the canonical store is a git repo, so git is a runtime dependency, and the wiki root must live on a mountable path.

OBJECTIVE: Add a multi-stage Dockerfile, a .dockerignore, and an entrypoint that can run either the MCP server or any `wiki` CLI command, with the wiki root on a volume path.

BUILD:
- Dockerfile (multi-stage, python:3.12-slim base):
    * Builder stage installs the package and its deps (prefer wheels; include any build deps Kùzu needs, falling back to the SQLite graph backend if a wheel is unavailable).
    * Runtime stage installs git and ca-certificates, creates a non-root user, copies the installed package, sets WIKI_ROOT=/data/wiki, and declares VOLUME /data/wiki.
- docker/entrypoint.sh:
    * Ensure /data/wiki and its subdirs exist; if it is not yet a git repo, `git init` and set a local user.name/user.email so commits never fail.
    * If the search index/graph are missing, run `wiki rebuild` to regenerate them (cache warm-up); never clear state.db.
    * Dispatch: first arg `serve` -> launch the MCP server; first arg `cli` -> exec `wiki` with the remaining args; otherwise exec the given command.
- .dockerignore excluding wiki/.index, .git of the source repo build context noise, __pycache__, .venv, tests artifacts.

CONSTRAINTS:
- Run as a non-root user. Never bake secrets (ANTHROPIC_API_KEY, MCP token) into the image — they come from env at run time.
- git MUST be present in the runtime image.
- The image must build and a stub-mode smoke command must run with no network.

ACCEPTANCE:
- `docker build -t mnesis .` succeeds. `docker run --rm mnesis cli --help` prints CLI help. `docker run --rm -e WIKI_LLM_STUB=1 mnesis cli rebuild` runs and exits 0 on an empty volume. The image runs as non-root (`docker run --rm mnesis id` shows a non-zero uid).

ON DONE: build the image, commit ("build: containerize mnesis with git-aware entrypoint"), report the final image size and the entrypoint dispatch options.
```

---

## Prompt D2 — Network transport for the MCP server

```
CONTEXT: The local MCP server runs over stdio, which a client spawns as a subprocess. A container deployment must expose it over the network, so add an HTTP transport while keeping stdio the default for local Claude Code.

OBJECTIVE: Make src/mnesis/mcp_server.py support a networked HTTP transport with a health endpoint and optional bearer-token auth, selectable by env.

BUILD:
- Verify the installed mcp SDK's transport API first, then add support for streamable HTTP (and/or SSE) alongside the existing stdio mode.
- Config (config.py): WIKI_MCP_TRANSPORT (stdio|http, default stdio), WIKI_MCP_HOST (default 0.0.0.0 in http mode), WIKI_MCP_PORT (default 8080), WIKI_MCP_TOKEN (optional bearer token; if set, all tool calls require it).
- A GET /health endpoint (http mode) returning status plus quick stats (page count, index present, graph present) — cheap, no LLM call.
- A module entrypoint so `python -m mnesis.mcp_server` honours the transport env; the entrypoint `serve` command (D1) uses it.

CONSTRAINTS:
- stdio behaviour is unchanged when WIKI_MCP_TRANSPORT is unset — no regression for local Claude Code.
- In http mode, if no token is configured, log a clear warning and do not bind beyond what the operator opts into; treat the endpoint as privileged (it can ingest and modify knowledge).
- Reuse the existing tool functions; this is transport only, no new tools.

ACCEPTANCE:
- tests/test_mcp_http.py (stub): start the server in http mode on an ephemeral port; GET /health returns 200 with stats; a tool call succeeds with a valid token and is rejected without one when a token is configured. stdio-mode tests still pass. `pytest -q` green.

ON DONE: run tests, commit ("feat: HTTP transport and health endpoint for MCP server"), report how to point an MCP client at the HTTP endpoint.
```

---

## Prompt D3 — Compose: core stack, volume, env

```
CONTEXT: The image builds and the server can run over HTTP. Compose the core single-service stack with durable storage and env-driven config.

OBJECTIVE: Add docker-compose.yml bringing up the wiki MCP server with a persistent volume, env file, healthcheck, and restart policy, plus a .env.example.

BUILD:
- docker-compose.yml:
    * service `wiki`: build ., command runs the MCP server in http mode (`serve`), env_file .env, environment defaults (WIKI_MCP_TRANSPORT=http, WIKI_ROOT=/data/wiki), ports "${WIKI_MCP_PORT:-8080}:8080", healthcheck hitting /health, restart: unless-stopped.
    * named volume `wiki-data` mounted at /data/wiki (holds pages, sources, .git, and .index).
    * A clearly-commented placeholder block noting where Tier-B services (Postgres+pgvector+AGE, Qdrant, Redis, Temporal) attach at Phases 5-6 — commented out, not active.
- .env.example: ANTHROPIC_API_KEY (blank), WIKI_LLM_MODEL, WIKI_LLM_STUB (1 for a no-network demo), WIKI_FILEBACK_THRESHOLD, WIKI_MCP_PORT, WIKI_MCP_TOKEN. Document that .env is gitignored.
- Enable SQLite WAL mode for the index and state DBs so the running server and an exec'd CLI can read concurrently (single-writer awareness; note heavy concurrent writes are a Tier-B concern).
- Makefile targets: docker-build, docker-up, docker-down, docker-logs, docker-cli (wraps `docker compose exec wiki wiki ...`).

CONSTRAINTS:
- The volume must survive `docker compose down` (only `down -v` removes it); state.db and git history are irreplaceable.
- No secrets in compose or the image; only via .env / the environment.
- Core stack stays single-service — do not activate Tier-B services.

ACCEPTANCE:
- `docker compose up -d` -> the wiki service becomes healthy. `make docker-cli ARGS="rebuild"` works. Ingest a stub source and query it via `make docker-cli`. `docker compose down && docker compose up -d` -> the ingested page is still there (volume persisted). `docker compose down -v` clears it.

ON DONE: bring it up, commit ("feat: docker-compose core stack with persistent volume"), report the up/seed/query/persist sequence you verified.
```

---

## Prompt D4 — Optional local-model profile (data isolation)

```
CONTEXT: The architecture's privacy story is local-first inference so sources never leave the trust boundary. Offer this as an opt-in Compose profile without making it the default.

OBJECTIVE: Add a `local-llm` Compose profile running a local model service, with the app able to target it for embeddings/generation.

BUILD:
- Compose profile `local-llm`:
    * service `ollama` (or an equivalent local model server) with its own named volume for model weights, and a one-shot init that pulls the configured model.
    * wire the `wiki` service, under this profile, to point llm.py at the local endpoint.
- Extend llm.py: a provider switch (env WIKI_LLM_PROVIDER=anthropic|local, default anthropic) with a local provider that calls the Ollama/OpenAI-compatible endpoint (WIKI_LLM_BASE_URL, WIKI_LLM_MODEL). The Anthropic path and the stub remain unchanged and default.
- Document in README that with this profile, ingestion and extraction run with no external inference calls.

CONSTRAINTS:
- Profile-gated: `docker compose up` (no profile) must NOT start the model service.
- Default behaviour (Anthropic / stub) is untouched.
- Keep model choice configurable; do not hardcode a large model that won't fit modest hardware — default to a small one and note how to change it.

ACCEPTANCE:
- `docker compose --profile local-llm up -d` starts the model service and pulls the model; `make docker-cli ARGS="ingest <sample>"` with WIKI_LLM_PROVIDER=local produces a page using local inference (verify no Anthropic call). Without the profile, the model service is absent.

ON DONE: commit ("feat: optional local-llm compose profile"), report the profile commands and the default-vs-local switch.
```

---

## Prompt D5 — Maintenance scheduler sidecar

```
CONTEXT: The wiki needs periodic upkeep (decay, graph lint, cache freshness). The in-app event hooks are Phase 4; until then, run the existing commands on a cadence at the deployment layer.

OBJECTIVE: Add a profile-gated `maintenance` service that periodically runs the maintenance commands against the shared volume.

BUILD:
- Compose profile `maintenance`: a `maintenance` service from the same image, sharing the wiki-data volume, running a small loop (or cron) that on an interval (env WIKI_MAINT_INTERVAL, default daily) runs: `wiki decay`, `wiki graph-lint --fix` (if Phase 3 is present), and a rebuild-if-missing check. Log each run with a summary.
- Guard each command so that if a capability isn't built yet (e.g. graph-lint pre-Phase-3) it is skipped cleanly, not errored.
- README note: this is deployment-level scheduling; Phase 4 moves it into the app as proper event hooks, at which point this sidecar can be retired.

CONSTRAINTS:
- Profile-gated; not started by a plain `up`.
- Commands run through the CLI (so changes are committed and audited via git in the shared volume).
- Single-writer discipline: the sidecar must tolerate the server running concurrently (WAL reads; brief write contention retried, not crashed).

ACCEPTANCE:
- `docker compose --profile maintenance up -d` -> the sidecar runs the cycle on start and on the interval; `git log` in the volume shows maintenance commits (e.g. status transitions) where applicable; running it on a fresh empty wiki is a clean no-op.

ON DONE: commit ("feat: maintenance scheduler sidecar"), report a sample cycle log.
```

---

## Prompt D6 — Seeding, demo, ops runbook, finalize

```
CONTEXT: The stack runs. Make a fresh `up` yield a populated, queryable wiki, document operations, and explain how an agent connects.

OBJECTIVE: Add a seed/bootstrap one-shot, an ops runbook (backup/restore, connecting Claude Code), and finalize docs.

BUILD:
- scripts/seed.py and a `docker-seed` Make target: a one-shot (`docker compose run --rm wiki cli ...` or a dedicated init service) that, in stub mode, ingests the bundled sample sources and runs `wiki rebuild`, so a clean deployment is immediately queryable. Idempotent (re-seeding does not duplicate).
- `docker-demo` target: run the latest phase's demo (demo_phase3 if present, else the highest available) inside the container against the seeded volume.
- README "Run with Docker" section: prerequisites; copy .env.example to .env; `make docker-build && make docker-up && make docker-seed`; query via `make docker-cli`; the optional profiles (local-llm, maintenance); and how to connect Claude Code to the HTTP MCP endpoint (e.g. `claude mcp add --transport http <url>` with the token header), contrasted with the local stdio .mcp.json.
- Ops runbook (docs/OPS.md): the durable-vs-regenerable table; backup = git bundle of the canonical layer + a copy of state.db (exclude .index, which `wiki rebuild` regenerates); restore = restore those, then rebuild; health checks; how to rotate WIKI_MCP_TOKEN.
- Confirm CLAUDE.md notes the deployment model and that .index is a regenerable cache while git history + state.db are durable.

CONSTRAINTS:
- Seeding and the demo run with no network (WIKI_LLM_STUB=1).
- The runbook backup must NOT depend on .index being present.

ACCEPTANCE:
- From a clean checkout: `make docker-build && make docker-up && make docker-seed && make docker-demo` yields a healthy, queryable system reachable over the MCP HTTP endpoint. A backup-then-restore drill (git bundle + state.db, drop .index, rebuild) reproduces the wiki with identical query results.

ON DONE: commit ("docs: docker seeding, ops runbook, and connection guide"), report the full clean-bring-up sequence and the backup/restore drill result.
```

---

## Verifying the deployment (after D6)

1. `make docker-build` — image builds; runs as non-root; git is present.
2. `make docker-up` — the `wiki` service reports healthy; `GET /health` returns stats.
3. `make docker-seed` then `make docker-cli ARGS='query "..."'` — a populated wiki answers queries.
4. Connect Claude Code to the HTTP MCP endpoint and call `wiki_query` / `wiki_ingest` as tools.
5. `docker compose down && docker compose up -d` — data survives (volume); `down -v` clears it.
6. `--profile local-llm up` — ingestion runs against the local model with no external inference call; plain `up` doesn't start it.
7. `--profile maintenance up` — the sidecar runs decay/lint on a cadence and commits results to the volume's git history.
8. Backup/restore drill — git bundle + `state.db`, delete `.index`, `wiki rebuild` — reproduces identical results, proving the durable/regenerable split holds in practice.

---

## Notes for running with Claude Code

- These prompts are independent of the feature phases — run D1 → D6 against whatever you have built; D5's commands self-skip capabilities that aren't present yet.
- The review judgement that matters: **treat the HTTP MCP endpoint as privileged.** It can ingest and modify the knowledge base, so it should run behind the token (and bound to localhost or a private network) rather than exposed openly. The prompts default to safe behaviour; don't loosen it for convenience.
- Verify the installed **mcp** SDK's HTTP-transport API before D2, and prefer a **Kùzu wheel**; the SQLite graph fallback (from Phase 3) keeps the image build from stalling on an embedded-engine quirk.
- When you reach Tier B (Phases 5–6), the commented service block in D3's compose is where Postgres+pgvector+AGE, Qdrant, Redis, and a durable workflow runner attach; the app's storage layer is swapped behind the same CLI/MCP surface, so the deployment grows rather than gets rebuilt.
```
