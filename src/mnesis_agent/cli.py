"""mnesis-agent command-line entry points.

Subcommands (one per archetype):
  mnesis-agent assistant                 interactive grounded REPL
  mnesis-agent research "<goal>"         bounded investigation → cited digest
  mnesis-agent ingest-daemon --watch DIR watch a directory, ingest new files

Shared plumbing (registry, provider, profile) lives in runner.py; this module
is the thin human-facing surface.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from . import config
from .audit import default_audit_log
from .daemon import IngestDaemon, IngestOutcome
from .local_tools import build_local_tool_source
from .memory import GroundedAgentResult
from .profiles import ASSISTANT, INGEST_DAEMON, RESEARCH
from .runner import build_registry, confirm_and_file, extract_digest_id, run_archetype


# ── Formatting helpers (kept pure for testing) ────────────────────────────────


def format_answer(result: GroundedAgentResult) -> str:
    """Render a grounded result as text: the answer plus a citations footer."""
    lines = [result.final_text or "(no answer produced)"]
    if result.citations:
        lines.append("")
        lines.append("Sources: " + ", ".join(f"[{c}]" for c in result.citations))
    if result.stop_reason not in ("end_turn",):
        lines.append(f"\n(stopped: {result.stop_reason})")
    return "\n".join(lines)


def _banner() -> None:
    print(
        f"mnesis-agent → MCP {config.MNESIS_MCP_URL} | "
        f"LLM {config.MNESIS_LLM_PROVIDER}/{config.MNESIS_LLM_MODEL}",
        file=sys.stderr,
    )


# ── assistant: interactive REPL ───────────────────────────────────────────────


async def _assistant_repl() -> None:
    registry = build_registry()  # local tools off: assistant can't use them anyway
    audit = default_audit_log()
    _banner()
    print("Mnesis assistant. Ask a question, or Ctrl-D / 'exit' to quit.")
    while True:
        try:
            line = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line or line.lower() in {"exit", "quit"}:
            if line.lower() in {"exit", "quit"}:
                break
            continue

        result = await run_archetype(ASSISTANT, line, registry, audit=audit)
        print("\n" + format_answer(result))

        # propose-only: surface a file-back proposal for the human to confirm.
        if result.proposal is not None:
            ans = input(
                "\nFile this answer back to Mnesis as a digest? [y/N] "
            ).strip().lower()
            if ans in {"y", "yes"}:
                raw = await confirm_and_file(result.proposal, registry)
                print(f"Filed: {raw}")
            else:
                print("Not filed.")


def cmd_assistant(_args: argparse.Namespace) -> int:
    asyncio.run(_assistant_repl())
    return 0


# ── research: bounded investigation ───────────────────────────────────────────


async def _research_run(goal: str) -> int:
    # Opt-in local tools (e.g. web_search) are added only if configured, and
    # are usable only by research (the policy layer enforces this).
    local = build_local_tool_source()
    registry = build_registry(local_tools=local)
    local_names = local.tool_names() if local is not None else frozenset()
    audit = default_audit_log()
    _banner()
    result = await run_archetype(
        RESEARCH, goal, registry, audit=audit, local_tool_names=local_names
    )
    print(format_answer(result))

    digest_id = extract_digest_id(result)
    if digest_id:
        print(f"\nCrystallized digest: {digest_id}")
    else:
        print("\nNo digest filed.", file=sys.stderr)
    return 0


def cmd_research(args: argparse.Namespace) -> int:
    return asyncio.run(_research_run(args.goal))


# ── ingest-daemon: directory watcher ──────────────────────────────────────────


def _log_outcome(outcome: IngestOutcome) -> None:
    print(
        f"[{outcome.status}] {outcome.path} "
        f"(ref={outcome.source_ref}"
        + (f", action={outcome.action}" if outcome.action else "")
        + (f", review={outcome.review_id}" if outcome.review_id is not None else "")
        + ")"
    )


async def _daemon_run(watch_dir: str, poll_interval: float) -> int:
    registry = build_registry()
    _banner()
    daemon = IngestDaemon(registry)
    print(f"Ingest daemon watching {watch_dir} (poll {poll_interval}s). Ctrl-C to stop.")
    try:
        await daemon.watch(watch_dir, poll_interval=poll_interval, on_outcome=_log_outcome)
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


def cmd_ingest_daemon(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return asyncio.run(_daemon_run(args.watch, args.poll_interval))


# ── argument parser ───────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mnesis-agent",
        description="Runtime agent that uses Mnesis as memory (via MCP).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_assistant = sub.add_parser("assistant", help="interactive grounded REPL")
    p_assistant.set_defaults(func=cmd_assistant)

    p_research = sub.add_parser("research", help="bounded investigation → cited digest")
    p_research.add_argument("goal", help="the research goal / question")
    p_research.set_defaults(func=cmd_research)

    p_daemon = sub.add_parser("ingest-daemon", help="watch a directory and ingest new files")
    p_daemon.add_argument("--watch", required=True, metavar="PATH", help="directory to watch")
    p_daemon.add_argument("--poll-interval", type=float, default=2.0, help="seconds between scans")
    p_daemon.set_defaults(func=cmd_ingest_daemon)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
