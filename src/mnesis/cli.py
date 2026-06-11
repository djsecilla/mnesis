"""The `mnesis` console entry point.

A thin argparse wrapper over the same tool functions the MCP server exposes
(:mod:`mnesis.mcp_server`). The CLI and MCP surfaces deliberately share that one
implementation so they can never drift apart — the compounding rules
(redaction-on-ingest, the file-back threshold, index upsert) live in one place.

Subcommands: ingest, query, get, file-back, list, rebuild.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import config, mcp_server


def _read_source(path: str) -> str:
    """Read a source from a file path, or stdin when ``path`` is ``-``."""
    if path == "-":
        return sys.stdin.read()
    return Path(path).read_text(encoding="utf-8")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mnesis", description="mnesis — a compounding knowledge base for AI agents."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="ingest a source file (or - for stdin)")
    p_ingest.add_argument("file", help="path to the source file, or - for stdin")
    p_ingest.add_argument(
        "--source-ref",
        "--ref",
        dest="source_ref",
        default=None,
        help="provenance id (default: the file stem, or 'stdin')",
    )

    p_query = sub.add_parser("query", help="keyword-search the wiki")
    p_query.add_argument("text", help="the search query")
    p_query.add_argument("--limit", type=int, default=10, help="max hits (default 10)")
    p_query.add_argument(
        "--include-stale",
        action="store_true",
        help="include stale pages (demoted) in results",
    )

    p_get = sub.add_parser("get", help="print a page's full Markdown by id")
    p_get.add_argument("id", help="the page id")

    p_fb = sub.add_parser("file-back", help="file a synthesized answer as a digest page")
    p_fb.add_argument("question")
    p_fb.add_argument("answer")
    p_fb.add_argument(
        "--score", type=float, default=None, help="quality score 0-1 (default: heuristic)"
    )

    sub.add_parser("list", help="list all pages")
    sub.add_parser("rebuild", help="rebuild the search index from Markdown")
    sub.add_parser("decay", help="recompute confidence and transition active<->stale")

    p_impact = sub.add_parser("impact", help="what depends on/uses an entity (graph)")
    p_impact.add_argument("entity", help="a type:value entity ref, e.g. library:redis")
    p_impact.add_argument("--depth", type=int, default=3, help="reverse-traversal depth (default 3)")

    p_entity = sub.add_parser("entity", help="inspect a graph entity and its edges")
    p_entity.add_argument("ref", help="a type:value entity ref, e.g. library:redis")

    p_neighbors = sub.add_parser("neighbors", help="adjacent entities of a graph entity")
    p_neighbors.add_argument("ref", help="a type:value entity ref")
    p_neighbors.add_argument("--pred", dest="predicate", default=None, help="filter by predicate")
    p_neighbors.add_argument(
        "--in", dest="incoming", action="store_true", help="incoming edges (default: outgoing)"
    )

    sub.add_parser("graph-stats", help="knowledge-graph node/edge counts")

    p_lint = sub.add_parser("graph-lint", help="check graph consistency; --fix applies safe fixes")
    p_lint.add_argument("--fix", action="store_true", help="apply the safe auto-fixes")

    sub.add_parser("review", help="list open contradiction reviews")

    p_resolve = sub.add_parser("resolve", help="resolve a contradiction review")
    p_resolve.add_argument("review_id", type=int, help="the review queue id")
    p_resolve.add_argument(
        "--keep", required=True, dest="keep_id", help="id of the page to keep (authoritative)"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    config.ensure_dirs()
    args = _build_parser().parse_args(argv)

    if args.command == "ingest":
        text = _read_source(args.file)
        source_ref = args.source_ref or ("stdin" if args.file == "-" else Path(args.file).stem)
        print(mcp_server.wiki_ingest(text, source_ref))
    elif args.command == "query":
        print(mcp_server.wiki_query(args.text, args.limit, include_stale=args.include_stale))
    elif args.command == "get":
        print(mcp_server.wiki_get(args.id))
    elif args.command == "file-back":
        print(mcp_server.wiki_file_back(args.question, args.answer, args.score))
    elif args.command == "list":
        print(mcp_server.wiki_list())
    elif args.command == "rebuild":
        print(mcp_server.wiki_rebuild())
    elif args.command == "decay":
        print(mcp_server.wiki_decay())
    elif args.command == "impact":
        print(mcp_server.wiki_impact(args.entity, depth=args.depth))
    elif args.command == "entity":
        print(mcp_server.wiki_entity(args.ref))
    elif args.command == "neighbors":
        print(mcp_server.wiki_neighbors(
            args.ref, predicate=args.predicate, direction="in" if args.incoming else "out"
        ))
    elif args.command == "graph-stats":
        print(mcp_server.wiki_graph_stats())
    elif args.command == "graph-lint":
        print(mcp_server.wiki_graph_lint(args.fix))
    elif args.command == "review":
        print(mcp_server.wiki_review())
    elif args.command == "resolve":
        print(mcp_server.wiki_resolve(args.review_id, args.keep_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
