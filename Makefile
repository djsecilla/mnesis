# mnesis — developer ergonomics. See README.md for the full runbook.
# All targets use uv; the test/demo targets run fully offline (stub LLM).

.PHONY: help setup test demo demo-phase2 run-mcp rebuild decay review

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

setup: ## Create the venv and install the package (editable) + deps
	uv venv
	uv pip install -e .

test: ## Run the full test suite offline (no network, no API key)
	WIKI_LLM_STUB=1 uv run pytest -q

demo: ## Run the Phase-1 end-to-end compounding-loop demo (offline, throwaway wiki)
	uv run python scripts/demo_end_to_end.py

demo-phase2: ## Run the Phase-2 lifecycle demo (offline, throwaway wiki)
	uv run python scripts/demo_phase2.py

run-mcp: ## Start the MCP server over stdio
	uv run python -m mnesis.mcp_server

rebuild: ## Rebuild the search index from the Markdown pages
	uv run mnesis rebuild

decay: ## Recompute confidence and transition pages active<->stale
	uv run mnesis decay

review: ## List open contradiction reviews
	uv run mnesis review
