"""Environment configuration for mnesis-agent.

Reads the same LLM env vars as the mnesis stack (same process environment) but
does NOT import mnesis.config — the agent is a separately deployable client.
"""
from __future__ import annotations

import os


def _bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


# ── MCP connection ──────────────────────────────────────────────────────────

#: URL of the Mnesis MCP HTTP endpoint (streamable-HTTP transport).
MNESIS_MCP_URL: str = os.environ.get("MNESIS_MCP_URL", "http://localhost:8080/mcp")

#: Bearer token for the Mnesis MCP endpoint (must match MNESIS_MCP_TOKEN on the server).
MNESIS_MCP_TOKEN: str = os.environ.get("MNESIS_MCP_TOKEN", "")

# ── LLM (mirrors mnesis stack; no import of mnesis.config) ─────────────────

MNESIS_LLM_PROVIDER: str = os.environ.get("MNESIS_LLM_PROVIDER", "anthropic")
MNESIS_LLM_MODEL: str = os.environ.get("MNESIS_LLM_MODEL", "claude-sonnet-4-6")
MNESIS_LLM_BASE_URL: str = os.environ.get("MNESIS_LLM_BASE_URL", "http://localhost:11434")
MNESIS_LLM_STUB: bool = _bool("MNESIS_LLM_STUB")
