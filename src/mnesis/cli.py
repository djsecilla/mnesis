"""The `mnesis` console entry point.

A thin argparse wrapper over the same tool functions the MCP server exposes
(:mod:`mnesis.mcp_server`). The CLI and MCP surfaces deliberately share that one
implementation so they can never drift apart — the compounding rules
(redaction-on-ingest, the file-back threshold, index upsert) live in one place.

Subcommands: ingest, query, get, file-back, list, rebuild.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import auth, config, mcp_server, tenancy


def _read_source(path: str) -> str:
    """Read a source from a file path, or stdin when ``path`` is ``-``."""
    if path == "-":
        return sys.stdin.read()
    return Path(path).read_text(encoding="utf-8")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mnesis", description="mnesis — a compounding knowledge base for AI agents."
    )
    parser.add_argument(
        "--tenant",
        default=config.DEFAULT_TENANT_ID,
        help=f"tenant to operate on (default: {config.DEFAULT_TENANT_ID})",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "migrate-tenants",
        help="move an existing single-store layout into tenants/default/ (idempotent)",
    )

    # Credential admin (T3): operates on the credential store, scoped to --tenant.
    p_auth = sub.add_parser("auth", help="issue/revoke/list tenant+principal credentials")
    asub = p_auth.add_subparsers(dest="auth_cmd", required=True)
    a_issue = asub.add_parser("issue", help="mint a credential (prints the token ONCE)")
    a_issue.add_argument("--principal", required=True, help="principal id (the actor)")
    a_issue.add_argument("--role", default="member", help="admin|member|readonly|agent")
    a_issue.add_argument("--name", default=None, help="optional human label")
    a_issue.add_argument("--expires-seconds", type=int, default=None, dest="expires_seconds",
                         help="optional lifetime in seconds (default: no expiry)")
    a_revoke = asub.add_parser("revoke", help="revoke a credential by id")
    a_revoke.add_argument("credential_id", help="the credential id (not the token)")
    asub.add_parser("list", help="list a tenant's credentials (no secrets)")

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

    sub.add_parser("health", help="read-only system health snapshot")

    p_dupes = sub.add_parser(
        "find-duplicates", help="heuristic near-duplicate candidate pairs (read-only)"
    )
    p_dupes.add_argument("--limit", type=int, default=20, help="max candidate pairs (default 20)")

    sub.add_parser("review", help="list open contradiction reviews")

    p_resolve = sub.add_parser("resolve", help="resolve a contradiction review")
    p_resolve.add_argument("review_id", type=int, help="the review queue id")
    p_resolve.add_argument(
        "--keep", required=True, dest="keep_id", help="id of the page to keep (authoritative)"
    )
    return parser


def _dispatch(args: argparse.Namespace) -> None:
    if args.command == "ingest":
        text = _read_source(args.file)
        source_ref = args.source_ref or ("stdin" if args.file == "-" else Path(args.file).stem)
        print(mcp_server.mnesis_ingest(text, source_ref))
    elif args.command == "query":
        print(mcp_server.mnesis_query(args.text, args.limit, include_stale=args.include_stale))
    elif args.command == "get":
        print(mcp_server.mnesis_get(args.id))
    elif args.command == "file-back":
        print(mcp_server.mnesis_file_back(args.question, args.answer, args.score))
    elif args.command == "list":
        print(mcp_server.mnesis_list())
    elif args.command == "rebuild":
        print(mcp_server.mnesis_rebuild())
    elif args.command == "decay":
        print(mcp_server.mnesis_decay())
    elif args.command == "impact":
        print(mcp_server.mnesis_impact(args.entity, depth=args.depth))
    elif args.command == "entity":
        print(mcp_server.mnesis_entity(args.ref))
    elif args.command == "neighbors":
        print(mcp_server.mnesis_neighbors(
            args.ref, predicate=args.predicate, direction="in" if args.incoming else "out"
        ))
    elif args.command == "graph-stats":
        print(mcp_server.mnesis_graph_stats())
    elif args.command == "graph-lint":
        print(mcp_server.mnesis_graph_lint(args.fix))
    elif args.command == "health":
        print(mcp_server.mnesis_health_report())
    elif args.command == "find-duplicates":
        print(mcp_server.mnesis_find_duplicates(args.limit))
    elif args.command == "review":
        print(mcp_server.mnesis_review())
    elif args.command == "resolve":
        print(mcp_server.mnesis_resolve(args.review_id, args.keep_id))


def _cmd_auth(args: argparse.Namespace) -> int:
    """Issue / revoke / list credentials for the ``--tenant`` (default ``default``)."""
    import time

    store = auth.CredentialStore()
    if args.auth_cmd == "issue":
        tenancy.create_tenant(args.tenant)  # ensure the tenant exists
        try:
            expires = (time.time() + args.expires_seconds) if args.expires_seconds else None
            raw, cred = store.issue(
                args.tenant, args.principal, args.role, expires_at=expires, name=args.name
            )
        except auth.AuthError as exc:
            print(f"error: {exc}")
            return 2
        print(f"issued credential {cred.id} for {args.tenant}/{args.principal} (role {cred.role})")
        print(f"  token (shown ONCE — store it securely): {raw}")
        return 0
    if args.auth_cmd == "revoke":
        ok = store.revoke(args.credential_id)
        print(f"revoked {args.credential_id}" if ok else f"not revoked (unknown or already revoked): {args.credential_id}")
        return 0 if ok else 1
    if args.auth_cmd == "list":
        creds = store.list_for_tenant(args.tenant)
        if not creds:
            print(f"no credentials for tenant '{args.tenant}'")
            return 0
        print(f"credentials for tenant '{args.tenant}':")
        for c in creds:
            state_str = "revoked" if c.revoked else ("active" if c.is_active() else "expired")
            print(f"  {c.id}  {c.principal_id:16} {c.role:9} {state_str}"
                  + (f"  ({c.name})" if c.name else ""))
        return 0
    return 2


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    # `migrate-tenants` runs against the data root directly (no tenant bound yet).
    if args.command == "migrate-tenants":
        result = tenancy.migrate_legacy_to_default()
        moved = ", ".join(result["moved"]) or "nothing to move"
        print(
            f"migrate-tenants: tenant '{result['tenant']}' "
            f"({'migrated' if result['migrated'] else 'already current'}; {moved}; "
            f"{result['pages']} pages)"
        )
        return 0

    # Credential admin operates on the credential store (outside any tenant root),
    # scoped to --tenant; no tenant binding is needed.
    if args.command == "auth":
        return _cmd_auth(args)

    # Every other command resolves + binds an authenticated (tenant, principal) at
    # this boundary; the store is unreachable until it is bound.
    try:
        ctx, principal = _resolve_data_context(args)
    except _CliDenied as exc:
        print(f"error: {exc}")
        return 2
    with tenancy.use(ctx):
        token = auth.bind_principal(principal) if principal is not None else None
        try:
            _dispatch(args)
        finally:
            if token is not None:
                auth.unbind_principal(token)
    return 0


class _CliDenied(Exception):
    """A tenant-scoped CLI op was refused (no resolved authenticated context)."""


def _resolve_data_context(args: argparse.Namespace):
    """Resolve the (TenantContext, Principal|None) a tenant-scoped CLI op runs under.

    Tenant identity comes from a verified credential, not the bare ``--tenant`` flag:
      - ``MNESIS_CREDENTIAL`` (an opaque token) → resolve it (tenant + principal);
        the credential's tenant is authoritative and any ``--tenant`` is ignored.
      - else when ``MNESIS_AUTH_ENABLED`` → **refuse** (fail closed): there is no
        unauthenticated way to reach a tenant's data.
      - else (legacy single-tenant, auth off) → the local ``--tenant`` (default
        ``default``) with no principal — the pre-multitenant convenience path.
    """
    token = os.environ.get("MNESIS_CREDENTIAL")
    if token:
        try:
            return auth.resolve_principal(token)
        except auth.AuthError as exc:
            raise _CliDenied(f"credential rejected ({exc}); set a valid MNESIS_CREDENTIAL") from exc
    if config.MNESIS_AUTH_ENABLED:
        raise _CliDenied(
            "authentication is enabled but no credential was provided; set "
            "MNESIS_CREDENTIAL to an issued token (the --tenant flag is not trusted)"
        )
    return tenancy.open_tenant(args.tenant), None


if __name__ == "__main__":
    raise SystemExit(main())
