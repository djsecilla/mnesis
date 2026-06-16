"""mnesis-agents entry point (LangGraph agentic layer).

Foundation only: reports the resolved model configuration. The agent graphs and
MCP-backed tools are wired in later prompts.
"""
from __future__ import annotations

from . import config


def main() -> None:
    print("mnesis-agents (LangGraph layer)")
    if config.MNESIS_AGENTS_STUB:
        print("  model       : STUB (deterministic offline fake — no keys, no network)")
    else:
        print(f"  provider    : {config.MNESIS_LLM_PROVIDER}")
        print(f"  model       : {config.MNESIS_LLM_MODEL or '(unset — set MNESIS_LLM_MODEL)'}")
        if config.MNESIS_LLM_BASE_URL:
            print(f"  base_url    : {config.MNESIS_LLM_BASE_URL}")
    print(f"  mcp endpoint: {config.MNESIS_MCP_URL}")
    print("  (agent graphs + MCP tools land in later prompts)")
