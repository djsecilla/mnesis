"""mnesis-agent entry point."""
from __future__ import annotations

from . import config


def main() -> None:
    print(f"mnesis-agent")
    print(f"  MCP endpoint : {config.MNESIS_MCP_URL}")
    print(f"  LLM provider : {config.MNESIS_LLM_PROVIDER} / {config.MNESIS_LLM_MODEL}")
    print("  (Interactive agent loop — see upcoming prompts)")
