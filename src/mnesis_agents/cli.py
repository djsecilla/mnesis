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
    if config.MNESIS_NOTES_ENABLED:
        print(f"  notes inbox : ENABLED ({config.MNESIS_NOTES_INBOX}, {config.MNESIS_NOTES_MODE} mode)")
    else:
        print("  notes inbox : disabled (MNESIS_NOTES_ENABLED=0)")
    print("  (use `mnesis-agents run` to start the runner, "
          "`mnesis-agents dream-cycle --now`, or `mnesis-agents ingest-note <file|dir>`)")


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


def register_notes_writer(
    registry, *, tools=None, connector=None, agent=None, pipeline=None,
    processed_store=None, dead_letter=None,
):
    """Register the notes-inbox connector + WritingAgent on ``registry``.

    Wires an event subscription whose handler runs the writing pipeline
    (dedup/retry/dead-letter) per inbound note. The connector and the agent share
    one ``ProcessedStore`` so a processed note is never re-ingested. Reaches Mnesis
    only over MCP (the injected/loaded tools). Returns ``(connector, sub,
    pipeline)`` — the connector is added to the runner's event triggers."""
    from .connectors.notes import NotesInboxConnector
    from .triggers.connector import ProcessedStore
    from .writing_agent import SourceWritingAgent
    from .writing_pipeline import WritingPipeline

    store = processed_store or ProcessedStore(
        config.MNESIS_AGENTS_CONNECTOR_STATE_DIR / "notes.sqlite"
    )
    if connector is None:
        connector = NotesInboxConnector(processed_store=store)
    if agent is None:
        if tools is None:
            tools = _load_mcp_tools()
        agent = SourceWritingAgent(tools=tools, processed_store=store)
    pipeline = pipeline or WritingPipeline(agent, dead_letter=dead_letter)

    # A file the connector cannot even read (unreadable/oversized) is dead-lettered
    # with a reason too — no silent loss at the detection boundary either.
    def _on_connector_error(err):
        try:
            pipeline.dead_letter.add(
                source_type=connector.name, source_ref=err.source_ref, content_hash=None,
                reason=f"connector/{err.error}: {err.detail}", attempts=0,
            )
        except Exception:  # noqa: BLE001
            pass

    connector._error_handler = _on_connector_error

    async def handler(event):
        return await pipeline.process_event(event)

    sub = registry.on_event("notes-writer", handler, source=connector.name)
    return connector, sub, pipeline


def _build_runner():
    """Assemble the runner. Registers the scheduled dream-cycle maintenance agent
    and the notes-inbox writing agent (each unless disabled); resilient — if Mnesis
    is unreachable at startup the runner still comes up rather than crashing."""
    from .registry import AgentRegistry
    from .runner import Runner

    log = logging.getLogger("mnesis_agents.runner")
    registry = AgentRegistry()
    event_triggers: list = []

    if config.MNESIS_AGENTS_DREAM_ENABLED:
        try:
            sub = register_maintenance_agent(registry)
            log.info("registered maintenance dream-cycle %r (%s)", sub.name, sub.schedule.describe())
        except Exception as exc:  # noqa: BLE001 — never let startup crash the runtime
            log.warning("could not register dream-cycle maintenance agent (%s); continuing", exc)

    if config.MNESIS_NOTES_ENABLED:
        try:
            connector, _sub, _pipeline = register_notes_writer(registry)
            event_triggers.append(connector)
            log.info("registered notes-inbox writer watching %s (%s mode)",
                     connector.inbox, connector.mode)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not register notes-inbox writer (%s); continuing", exc)

    return Runner(registry, event_triggers=event_triggers)


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


def _build_action_gate():
    """Build the approval gate over the default inert channels. Tests inject a
    gate with fakes instead."""
    from .action_gate import ActionGate
    from .channels import default_channel_registry

    return ActionGate(default_channel_registry())


def cmd_actions(args: argparse.Namespace) -> int:
    """The approvals surface: list pending action proposals and approve/edit/reject
    them. No channel executes without an explicit approval here."""
    from pathlib import Path as _Path

    gate = _build_action_gate()
    sub = args.subcommand or "list"

    if sub == "list":
        pending = gate.store.list_pending()
        if not pending:
            print("no pending action proposals")
            return 0
        print(f"pending action proposals ({len(pending)}):")
        for p in pending:
            print(f"  {p.summary()}")
            if p.rationale:
                print(f"      rationale: {p.rationale}")
        return 0

    if not args.proposal_id:
        print(f"error: `actions {sub}` requires a proposal id")
        return 2

    if sub == "approve":
        patch: dict = {}
        if args.title:
            patch["title"] = args.title
        if args.body_file:
            patch["body"] = _Path(args.body_file).read_text(encoding="utf-8")
        try:
            res = gate.approve(
                args.proposal_id,
                edited_artifact=patch or None,
                edited_destination=args.destination,
                note=args.note,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"error: {exc}")
            return 2
        print(f"{res.status}: {args.proposal_id} via {res.channel} -> {res.location or res.error}")
        return 0 if res.ok else 1

    if sub == "reject":
        try:
            gate.reject(args.proposal_id, reason=args.reason or "")
        except Exception as exc:  # noqa: BLE001
            print(f"error: {exc}")
            return 2
        print(f"rejected: {args.proposal_id} (nothing delivered)")
        return 0
    return 2


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

    p_act = sub.add_parser("actions", help="approval gate: list/approve/reject pending action proposals")
    p_act.add_argument("subcommand", nargs="?", choices=["list", "approve", "reject"], default="list")
    p_act.add_argument("proposal_id", nargs="?", help="the proposal id (for approve/reject)")
    p_act.add_argument("--destination", help="override the destination on approval (human input)")
    p_act.add_argument("--title", help="edit the artifact title on approval")
    p_act.add_argument("--body-file", dest="body_file", help="replace the artifact body from a file")
    p_act.add_argument("--note", help="a decision note recorded with the approval")
    p_act.add_argument("--reason", help="a reason recorded with a rejection")
    p_act.set_defaults(func=cmd_actions)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "func", None):
        return args.func(args)
    _print_info()
    return 0
