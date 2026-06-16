"""mnesis-agents entry point (LangGraph agentic layer).

Subcommands:
  (default)        report the resolved model / MCP configuration.
  run              start the runner with whatever agents are registered
                   (zero in this scaffold = a healthy idle runner).
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from . import config


def _print_info() -> None:
    print("mnesis-agents (LangGraph layer)")
    if config.MNESIS_AGENTS_STUB:
        print("  model       : STUB (deterministic offline fake — no keys, no network)")
    else:
        print(f"  provider    : {config.MNESIS_LLM_PROVIDER}")
        print(f"  model       : {config.MNESIS_LLM_MODEL or '(unset — set MNESIS_LLM_MODEL)'}")
        if config.MNESIS_LLM_BASE_URL:
            print(f"  base_url    : {config.MNESIS_LLM_BASE_URL}")
    print(f"  mcp endpoint: {config.MNESIS_MCP_URL}")
    print("  (use `mnesis-agents run` to start the runner)")


def _build_runner():
    """Assemble the runner from registered agents. In this scaffold the registry
    is empty (no concrete agents/connectors), so the runner starts idle."""
    from .registry import AgentRegistry
    from .runner import Runner

    registry = AgentRegistry()
    # Concrete agents/connectors register here in later prompts.
    return Runner(registry)


def cmd_run(_args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    runner = _build_runner()
    if runner.registry.is_empty:
        logging.getLogger("mnesis_agents.runner").info(
            "no agents registered — starting an idle runner (Ctrl-C to stop)"
        )
    asyncio.run(runner.serve_forever())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mnesis-agents", description="Mnesis LangGraph agentic layer.")
    sub = parser.add_subparsers(dest="command")
    p_run = sub.add_parser("run", help="start the runner (idle if no agents are registered)")
    p_run.set_defaults(func=cmd_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "func", None):
        return args.func(args)
    _print_info()
    return 0
