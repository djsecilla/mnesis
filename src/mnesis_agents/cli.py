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
    if config.MNESIS_AGENTS_DREAM_ENABLED:
        print(f"  dream cycle : ENABLED ({_runner_dream_schedule().describe()})")
    else:
        print("  dream cycle : disabled (MNESIS_AGENTS_DREAM_ENABLED=0)")
    print("  (use `mnesis-agents run` to start the runner, "
          "or `mnesis-agents dream-cycle --now` to run one now)")


def _load_mcp_tools():
    """Load the Mnesis tools from the live MCP endpoint (read + maintenance +
    write, so the dream cycle can crystallize). Tests inject fakes instead."""
    from .knowledge import ToolRegistry, mnesis_mcp_source

    return asyncio.run(ToolRegistry([mnesis_mcp_source()]).get_tools())


def _runner_dream_schedule():
    """The dream-cycle cadence the bundled (interval-only) runner uses.

    Prefers an explicit ``MNESIS_AGENTS_DREAM_INTERVAL_SECONDS``; otherwise
    approximates the nightly cron as a daily interval (precise cron timing needs
    the APScheduler extra, which the bundled scheduler does not require)."""
    from .triggers.schedule import Schedule

    secs = config.MNESIS_AGENTS_DREAM_INTERVAL_SECONDS or 86400.0
    return Schedule(interval_seconds=secs)


def register_maintenance_agent(registry, *, tools=None, schedule=None):
    """Register the scheduled dream-cycle MaintenanceAgent on ``registry``.

    Reaches Mnesis only over MCP (the injected/loaded tools). The single owner of
    periodic maintenance now that the D5 sidecar is retired."""
    from .maintenance_agent import DreamMaintenanceAgent, register_dream_cycle

    if tools is None:
        tools = _load_mcp_tools()
    agent = DreamMaintenanceAgent(tools=tools)
    return register_dream_cycle(registry, agent, schedule=schedule or _runner_dream_schedule())


def _build_runner():
    """Assemble the runner. Registers the scheduled dream-cycle maintenance agent
    (unless disabled); resilient — if Mnesis is unreachable at startup the runner
    still comes up idle rather than crashing."""
    from .registry import AgentRegistry
    from .runner import Runner

    registry = AgentRegistry()
    if config.MNESIS_AGENTS_DREAM_ENABLED:
        try:
            sub = register_maintenance_agent(registry)
            logging.getLogger("mnesis_agents.runner").info(
                "registered maintenance dream-cycle %r (%s)", sub.name, sub.schedule.describe()
            )
        except Exception as exc:  # noqa: BLE001 — never let startup crash the runtime
            logging.getLogger("mnesis_agents.runner").warning(
                "could not register dream-cycle maintenance agent (%s); runner stays idle", exc
            )
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


def _build_writing_agent():
    """Build a writing agent over the LIVE Mnesis MCP tools. Tests monkeypatch
    this to inject fakes."""
    from .knowledge import ToolRegistry, mnesis_mcp_source
    from .writing_agent import SourceWritingAgent

    tools = asyncio.run(ToolRegistry([mnesis_mcp_source()]).get_tools())
    return SourceWritingAgent(tools=tools)


def cmd_ingest_note(args: argparse.Namespace) -> int:
    """On-demand: run the writing pipeline over a file or directory immediately
    (backfills/tests). Same path as the live connector — dedup/retry/dead-letter
    all apply."""
    from .writing_pipeline import WritingPipeline, ingest_note_paths

    agent = _build_writing_agent()
    pipeline = WritingPipeline(agent)
    results = asyncio.run(ingest_note_paths(args.paths, agent=agent, pipeline=pipeline))
    if not results:
        print("no notes found to ingest")
        return 0
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
        line = f"  {r.status:16} {r.source_ref}"
        if r.action:
            line += f"  ({r.action})"
        if r.status == "dead_letter":
            line += f"  reason: {r.error}"
        print(line)
    print("summary: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    dead = pipeline.dead_letter.all()
    if dead:
        print(f"dead-letter: {len(dead)} item(s) — inspect {pipeline.dead_letter._path}")
    return 0


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

    p_in = sub.add_parser("ingest-note", help="ingest a note file or directory on demand (backfill)")
    p_in.add_argument("paths", nargs="+", help="one or more .md/.txt files or directories")
    p_in.set_defaults(func=cmd_ingest_note)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "func", None):
        return args.func(args)
    _print_info()
    return 0
