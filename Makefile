# mnesis — developer ergonomics. See README.md for the full runbook.
# All targets use uv; the test/demo targets run fully offline (stub LLM).

.PHONY: help setup test demo demo-phase2 demo-phase3 run-mcp rebuild decay review graph-stats graph-lint \
        docker-build docker-up docker-down docker-logs docker-cli docker-seed docker-demo ui-dev \
        agent-up agent-down agent-logs agent-research agent-assistant

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

setup: ## Create the venv and install the package (editable) + deps
	uv venv
	uv pip install -e .

test: ## Run the full test suite offline (no network, no API key)
	MNESIS_LLM_STUB=1 uv run pytest -q

demo: ## Run the Phase-1 end-to-end compounding-loop demo (offline, throwaway wiki)
	uv run python scripts/demo_end_to_end.py

demo-phase2: ## Run the Phase-2 lifecycle demo (offline, throwaway wiki)
	uv run python scripts/demo_phase2.py

demo-phase3: ## Run the Phase-3 graph demo (offline, throwaway wiki)
	uv run python scripts/demo_phase3.py

run-mcp: ## Start the MCP server over stdio
	uv run python -m mnesis.mcp_server

rebuild: ## Rebuild the search index from the Markdown pages
	uv run mnesis rebuild

decay: ## Recompute confidence and transition pages active<->stale
	uv run mnesis decay

review: ## List open contradiction reviews
	uv run mnesis review

graph-stats: ## Print knowledge-graph node/edge counts
	uv run mnesis graph-stats

graph-lint: ## Check graph consistency (use ARGS=--fix to apply safe fixes)
	uv run mnesis graph-lint $(ARGS)

# --- Docker / compose -------------------------------------------------------

docker-build: ## Build the container images (mnesis + web UI)
	docker compose build

docker-up: ## Start the stack incl. Web UI (detached); volume persists across down
	docker compose up -d

docker-down: ## Stop the stack (volume KEPT; use `docker compose down -v` to wipe)
	docker compose down

docker-logs: ## Tail the mnesis + mnesis-ui service logs
	docker compose logs -f mnesis mnesis-ui

ui-dev: ## Run the Vite dev server against a running mnesis (http://localhost:5173)
	cd ui && npm install && npm run dev

docker-cli: ## Run a CLI command in the running container, e.g. ARGS='query "redis"'
	docker compose exec -T mnesis mnesis $(ARGS)

docker-seed: ## Seed the volume with bundled sample sources (offline, idempotent)
	docker compose run --rm -e MNESIS_LLM_STUB=1 --entrypoint python mnesis -m mnesis.seed

docker-demo: ## Run the latest-phase demo inside the container (offline, self-contained)
	@d=$$(for s in demo_phase3 demo_phase2 demo_end_to_end; do [ -f scripts/$$s.py ] && echo $$s && break; done); \
	echo "running scripts/$$d.py in the container"; \
	docker compose run --rm -e MNESIS_LLM_STUB=1 -v "$(PWD)/scripts:/scripts:ro" \
		--entrypoint python mnesis "/scripts/$$d.py"

# --- Agent layer ------------------------------------------------------------

agent-up: ## Start the ingest-daemon agent alongside mnesis (docker compose --profile agent)
	docker compose --profile agent up -d

agent-down: ## Stop the agent service (mnesis stays up)
	docker compose rm -sf mnesis-agent

agent-logs: ## Tail the ingest-daemon logs
	docker compose logs -f mnesis-agent

agent-research: ## Bounded research investigation against the running stack: make agent-research GOAL="what uses redis"
	docker compose run --rm mnesis-agent agent research "$(GOAL)"

agent-assistant: ## Interactive grounded assistant REPL against the running stack
	docker compose run --rm mnesis-agent agent assistant
