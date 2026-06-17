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


def _build_dream_agent(plan=None, crystallize=None):
    """Build a dream-cycle agent over the LIVE Mnesis MCP tools (read+maintenance
    +write, for crystallization). Tests monkeypatch this to inject fake tools."""
    from .knowledge import ToolRegistry, mnesis_mcp_source
    from .maintenance_agent import DreamMaintenanceAgent

    tools = asyncio.run(ToolRegistry([mnesis_mcp_source()]).get_tools())
    return DreamMaintenanceAgent(tools=tools, plan=plan, crystallize=crystallize)


def cmd_dream_cycle(args: argparse.Namespace) -> int:
    """Run the maintenance dream cycle on demand (--now), or show the latest
    persisted report (--report / default)."""
    from .reports import DreamReportStore, format_summary

    if args.now:
        plan = [p.strip() for p in args.plan.split(",") if p.strip()] if args.plan else None
        crystallize = True if args.crystallize else None  # else config default (off)
        report = _build_dream_agent(plan=plan, crystallize=crystallize).run_and_record()
        print(format_summary(report))
        return 0

    summary = DreamReportStore().latest_summary()
    print(summary or "No dream cycle has run yet (use --now to run one).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mnesis-agents", description="Mnesis LangGraph agentic layer.")
    sub = parser.add_subparsers(dest="command")
    p_run = sub.add_parser("run", help="start the runner (idle if no agents are registered)")
    p_run.set_defaults(func=cmd_run)

    p_dc = sub.add_parser("dream-cycle", help="run the maintenance dream cycle, or show its latest report")
    p_dc.add_argument("--now", action="store_true", help="run a dream cycle now (on demand)")
    p_dc.add_argument("--report", action="store_true", help="show the latest persisted report (default)")
    p_dc.add_argument("--crystallize", action="store_true", help="file a maintenance digest back into Mnesis")
    p_dc.add_argument("--plan", default=None, help="comma-separated pass plan (default: the standard plan)")
    p_dc.set_defaults(func=cmd_dream_cycle)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "func", None):
        return args.func(args)
    _print_info()
    return 0
