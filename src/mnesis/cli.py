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

from . import admin, audit, auth, authz, cli_auth, config, identity, mcp_server, providers, store, tenancy, tokens


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
    parser.add_argument(
        "--token",
        default=None,
        help="a PAT or session token for headless auth (else MNESIS_TOKEN, else the "
             "token stored by `mnesis login`).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # Interactive auth (IAM6): log in with a password (IAM2) → a stored session token.
    p_login = sub.add_parser("login", help="log in with a password; stores a local session token")
    p_login.add_argument("--principal", "--username", dest="principal", required=True,
                         help="the username / principal id")
    p_login.add_argument("--password", default=None,
                         help="the password (else MNESIS_PASSWORD, else an interactive prompt)")
    sub.add_parser("logout", help="revoke the stored session and clear the local credential")
    sub.add_parser("whoami", help="show the currently-authenticated principal")

    # User lifecycle (IAM8) — tenant-admin scoped (within --tenant); PDP-enforced.
    p_user = sub.add_parser("user", help="manage users in --tenant (tenant-admin only)")
    usub = p_user.add_subparsers(dest="user_cmd", required=True)
    u_prov = usub.add_parser("provision", help="create a user with a role + password")
    u_prov.add_argument("principal", help="the user's principal id")
    u_prov.add_argument("--role", default="member", help="admin|member|readonly|agent")
    u_prov.add_argument("--password", default=None, help="password (else MNESIS_NEW_USER_PASSWORD/prompt)")
    u_deact = usub.add_parser("deactivate", help="force-revoke all a user's credentials + tokens")
    u_deact.add_argument("principal", help="the user's principal id")
    u_role = usub.add_parser("set-role", help="assign a role to a user")
    u_role.add_argument("principal", help="the user's principal id")
    u_role.add_argument("role", help="admin|member|readonly|agent")
    usub.add_parser("list", help="list the tenant's users (no secrets)")

    # Personal Access Tokens (IAM6/IAM3): headless automation credentials.
    p_pat = sub.add_parser("pat", help="manage personal access tokens (headless automation)")
    ptsub = p_pat.add_subparsers(dest="pat_cmd", required=True)
    pt_create = ptsub.add_parser("create", help="mint a scoped PAT (prints the token ONCE)")
    pt_create.add_argument("--name", required=True, help="a label for the PAT")
    pt_create.add_argument("--scope", action="append", default=[], dest="scopes",
                           help="a permission scope to grant, repeatable (default: read)")
    pt_create.add_argument("--ttl", type=int, default=None, help="lifetime in seconds (default: 90 days)")
    ptsub.add_parser("list", help="list your PATs (no secrets)")
    pt_revoke = ptsub.add_parser("revoke", help="revoke one of your tokens by id")
    pt_revoke.add_argument("token_id", help="the token id (not the token)")

    sub.add_parser(
        "migrate-tenants",
        help="move an existing single-store layout into tenants/default/ (idempotent)",
    )

    # First-run deploy bootstrap (IAM8): create the first tenant-admin web user so a
    # fresh deployment has a real login. Guarded/idempotent; no default password.
    p_init = sub.add_parser("init-admin", help="create the first tenant-admin web user (first-run)")
    p_init.add_argument("--principal", default="admin", help="the admin user's principal id")
    p_init.add_argument("--password", default=None, help="password (else MNESIS_WEB_ADMIN_PASSWORD)")

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

    # System-admin tenant lifecycle (T7). All but `bootstrap` require a system-admin
    # credential in MNESIS_ADMIN_CREDENTIAL; tenant principals can never manage tenants.
    p_admin = sub.add_parser("admin", help="system-admin tenant lifecycle (provision/list/suspend/delete)")
    adsub = p_admin.add_subparsers(dest="admin_cmd", required=True)
    ad_boot = adsub.add_parser("bootstrap", help="create the first system-admin (local root of trust)")
    ad_boot.add_argument("--principal", default="root", help="the admin principal id")
    ad_boot.add_argument(
        "--password", default=None,
        help="operator-supplied password for a PASSWORD system-admin (IAM2); else a "
             "random TOKEN credential is minted. May come from MNESIS_BOOTSTRAP_PASSWORD "
             "or an interactive prompt. Never a default.",
    )
    ad_csa = adsub.add_parser("create-system-admin", help="create another system-admin (password)")
    ad_csa.add_argument("--principal", required=True, help="the new system-admin's principal id")
    ad_csa.add_argument("--password", default=None, help="password (else MNESIS_NEW_ADMIN_PASSWORD/prompt)")
    ad_prov = adsub.add_parser("provision", help="create a tenant + its initial admin credential")
    ad_prov.add_argument("tenant_id")
    ad_prov.add_argument("--name", default=None)
    adsub.add_parser("list", help="list all tenants")
    ad_susp = adsub.add_parser("suspend", help="deny access while retaining data")
    ad_susp.add_argument("tenant_id")
    ad_res = adsub.add_parser("resume", help="restore access to a suspended tenant")
    ad_res.add_argument("tenant_id")
    ad_q = adsub.add_parser("set-quota", help="set a tenant's resource quotas (0 = unlimited)")
    ad_q.add_argument("tenant_id")
    ad_q.add_argument("--max-pages", type=int, default=None, dest="max_pages")
    ad_q.add_argument("--max-bytes", type=int, default=None, dest="max_bytes")
    ad_del = adsub.add_parser("delete", help="remove a tenant's data/credentials/agent state")
    ad_del.add_argument("tenant_id")
    ad_del.add_argument("--confirm", required=True, help="must equal the tenant id (guard)")

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

    p_mokf = sub.add_parser(
        "migrate-okf",
        help="rewrite this tenant's pages into OKF form (lossless, idempotent, reversible)",
    )
    p_mokf.add_argument("--dry-run", action="store_true", dest="dry_run",
                        help="report what would change; write nothing")
    p_mokf.add_argument("--rollback", action="store_true",
                        help="restore the pre-migration state from the backup ref")

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


# Per-command PDP action (IAM6). Every data command enforces this against the
# resolved principal before it runs; the same coarse actions the other surfaces use.
_COMMAND_PERMISSION: dict[str, str] = {
    "query": authz.READ, "get": authz.READ, "list": authz.READ, "impact": authz.READ,
    "entity": authz.READ, "neighbors": authz.READ, "graph-stats": authz.READ,
    "health": authz.READ, "find-duplicates": authz.READ, "review": authz.READ,
    "ingest": authz.WRITE, "file-back": authz.WRITE,
    "rebuild": authz.MAINTAIN, "decay": authz.MAINTAIN, "graph-lint": authz.MAINTAIN,
    "resolve": authz.MAINTAIN, "migrate-okf": authz.MAINTAIN,
}


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
    elif args.command == "migrate-okf":
        if args.rollback:
            try:
                res = store.rollback_okf_migration()
            except store.MigrationError as exc:
                print(f"error: {exc}")
                return
            print(f"rolled back OKF migration for '{res['tenant']}' to {res['rolled_back_to'][:10]}")
            return
        rep = store.migrate_to_okf(dry_run=args.dry_run)
        verb = "would convert" if args.dry_run else ("converted" if rep["committed"] else "no changes")
        ids = ", ".join(rep["converted"]) or "(none)"
        print(f"OKF migration [{rep['tenant']}]: {verb} {len(rep['converted'])} page(s): {ids}")
        if rep["already_conformant"]:
            print("  already OKF-conformant — nothing to do")
        elif rep.get("backup_ref"):
            print(f"  backup ref: {rep['backup_ref'][:10]} (roll back with `mnesis migrate-okf --rollback`)")


def _cmd_login(args: argparse.Namespace) -> int:
    """Log in with a password (IAM2) and store a session token (IAM3) locally.

    The tenant is the ``--tenant`` (default ``default``); identity is proven against the
    local password provider. Never prints the token."""
    import getpass

    tenant, username = args.tenant, args.principal
    password = args.password or os.environ.get("MNESIS_PASSWORD")
    if not password and sys.stdin.isatty():
        password = getpass.getpass(f"password for {tenant}/{username}: ")
    if not password:
        print("error: a password is required (--password, MNESIS_PASSWORD, or an interactive prompt)")
        return 2

    try:
        principal = providers.LocalPasswordProvider().authenticate(tenant, username, password)
    except providers.AccountLocked as exc:
        print(f"error: account locked after too many attempts; try again in ~{int(exc.retry_after)}s")
        return 2
    except identity.AuthError:
        print("error: invalid username or password")
        return 2

    raw, rec = tokens.TokenService().issue_session(principal)
    audit.record("session_issued", tenant_id=principal.tenant_id, principal_id=principal.principal_id,
                 credential_id=rec.id, action="login", result="ok")
    store = cli_auth.CliCredentialStore()
    store.save(raw, tenant_id=principal.tenant_id, principal_id=principal.principal_id,
               roles=principal.roles)
    print(f"logged in as {principal.principal_id} (tenant {principal.tenant_id}, "
          f"roles {', '.join(sorted(principal.roles))})")
    print(f"  session stored at {store.path} (mode 0600); re-run `mnesis login` when it expires")
    return 0


def _cmd_logout(_args: argparse.Namespace) -> int:
    """Revoke the stored session server-side and clear the local credential file."""
    store = cli_auth.CliCredentialStore()
    raw = store.token()
    if not raw:
        print("not logged in")
        return 0
    try:
        tokens.TokenService().revoke_token(raw)  # immediate, server-side
        stored = store.load() or {}
        audit.record("session_revoked", tenant_id=stored.get("tenant_id"),
                     principal_id=stored.get("principal_id"), action="logout", result="ok")
    except Exception:  # noqa: BLE001 — always clear the local file regardless
        pass
    store.clear()
    print("logged out (session revoked and local credential cleared)")
    return 0


def _cmd_whoami(args: argparse.Namespace) -> int:
    """Show the currently-authenticated principal (or report not logged in)."""
    try:
        ctx, principal = _resolve_optional(args)
    except _CliDenied as exc:
        print(f"error: {exc}")
        return 2
    if principal is None:
        print("not logged in (run `mnesis login`, or set MNESIS_TOKEN / MNESIS_CREDENTIAL)")
        return 1
    perms = ", ".join(sorted(authz.effective_permissions(principal))) or "(none)"
    print(f"{principal.principal_id} @ {principal.tenant_id}")
    print(f"  roles: {', '.join(sorted(principal.roles))}")
    print(f"  permissions: {perms}")
    return 0


def _cmd_pat(args: argparse.Namespace) -> int:
    """Manage the caller's Personal Access Tokens (headless automation)."""
    try:
        _ctx, principal = _resolve_optional(args)
    except _CliDenied as exc:
        print(f"error: {exc}")
        return 2
    if principal is None:
        print("error: not authenticated — run `mnesis login` first to manage PATs")
        return 2

    svc = tokens.TokenService()
    if args.pat_cmd == "create":
        scopes = tuple(args.scopes) or (authz.READ,)  # default least-privilege: read only
        try:
            raw, rec = svc.issue_pat(principal, args.name, scopes, ttl=args.ttl)
        except tokens.ScopeError as exc:
            print(f"error: {exc}")
            return 2
        audit.record("token_issued", tenant_id=principal.tenant_id, principal_id=principal.principal_id,
                     credential_id=rec.id, action="pat:create", result="ok", token_type="pat")
        print(f"created PAT {rec.id} '{rec.name}' (scopes {', '.join(rec.scopes)})")
        print(f"  token (shown ONCE — store it securely): {raw}")
        return 0
    if args.pat_cmd == "list":
        pats = [t for t in svc.list_for_principal(principal.tenant_id, principal.principal_id)
                if t.token_type == tokens.PAT]
        if not pats:
            print("no PATs")
            return 0
        for t in pats:
            state = "revoked" if t.revoked_at else "active"
            print(f"  {t.id}  {t.name or '-':16} [{', '.join(t.scopes) or 'read'}]  {state}")
        return 0
    if args.pat_cmd == "revoke":
        ok = svc.revoke(args.token_id)
        if ok:
            audit.record("token_revoked", tenant_id=principal.tenant_id,
                         principal_id=principal.principal_id, credential_id=args.token_id,
                         action="pat:revoke", result="ok")
        print(f"revoked {args.token_id}" if ok else f"nothing to revoke: {args.token_id}")
        return 0 if ok else 1
    return 2


def _cmd_user(args: argparse.Namespace) -> int:
    """Tenant-admin user lifecycle (IAM8) — always within the CALLER'S tenant (a
    tenant-admin can never reach another tenant). PDP-enforced + audited."""
    try:
        _ctx, actor = _resolve_optional(args)
    except _CliDenied as exc:
        print(f"error: {exc}")
        return 2
    if actor is None:
        print("error: not authenticated — run `mnesis login` as a tenant-admin")
        return 2
    tenant = actor.tenant_id  # never a --tenant override: you manage only your tenant
    try:
        if args.user_cmd == "provision":
            password = args.password or os.environ.get("MNESIS_NEW_USER_PASSWORD")
            if not password and sys.stdin.isatty():
                import getpass
                password = getpass.getpass(f"password for {tenant}/{args.principal}: ")
            if not password:
                print("error: a password is required (--password / MNESIS_NEW_USER_PASSWORD / prompt)")
                return 2
            admin.provision_user(tenant, args.principal, args.role, password, actor=actor)
            print(f"provisioned user '{args.principal}' (role {args.role}) in tenant '{tenant}'")
            return 0
        if args.user_cmd == "deactivate":
            res = admin.deactivate_user(tenant, args.principal, actor=actor)
            print(f"deactivated '{args.principal}': force-revoked "
                  f"{res['credentials_revoked']} credential(s) + {res['tokens_revoked']} token(s)")
            return 0
        if args.user_cmd == "set-role":
            n = admin.set_user_role(tenant, args.principal, args.role, actor=actor)
            print(f"assigned role '{args.role}' to '{args.principal}' ({n} credential(s) updated)")
            return 0
        if args.user_cmd == "list":
            users = admin.list_users(tenant, actor=actor)
            if not users:
                print(f"no users in tenant '{tenant}'")
                return 0
            print(f"users in tenant '{tenant}':")
            for u in users:
                state = "active" if u["active"] else "inactive"
                print(f"  {u['principal_id']:16} {','.join(u['roles']):22} {state}")
            return 0
    except admin.UserManagementError as exc:
        print(f"error: {exc}")
        return 3
    except auth.AuthError as exc:  # password policy / invalid role
        print(f"error: {exc}")
        return 2
    return 2


def _cmd_auth(args: argparse.Namespace) -> int:
    """Issue / revoke / list credentials for the ``--tenant`` (default ``default``).

    Admin-only (IAM6): the caller must resolve to a principal that may
    ``credentials:issue`` for the target tenant. In legacy (auth-off) mode with no
    principal, nothing is narrowed."""
    import time

    # Enforce the admin role on credential management (PDP), when a caller is resolved.
    try:
        _ctx, caller = _resolve_optional(args)
    except _CliDenied as exc:
        print(f"error: {exc}")
        return 2
    if caller is not None and not authz.authorize(
        caller, authz.CREDENTIALS_ISSUE, context={"tenant_id": args.tenant}
    ):
        print(f"error: '{caller.principal_id}' (role {caller.role}) may not manage "
              f"credentials for tenant '{args.tenant}'")
        return 3

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


def _cmd_admin(args: argparse.Namespace) -> int:
    """System-admin tenant lifecycle (T7). ``bootstrap`` mints the root-of-trust
    locally; every other op requires MNESIS_ADMIN_CREDENTIAL to resolve to a
    system-admin (fail closed) and is audited in the system audit log."""
    if args.admin_cmd == "bootstrap":
        # IAM2: a password bootstrap when the operator supplies one (flag > env > prompt);
        # otherwise the legacy random-token root of trust. Never a hardcoded default.
        password = args.password or config.MNESIS_BOOTSTRAP_PASSWORD
        if password is None and sys.stdin.isatty():
            import getpass
            entered = getpass.getpass("system-admin password (leave blank to mint a token instead): ")
            password = entered or None
        if password:
            try:
                cred = admin.bootstrap_system_admin(args.principal, password)
            except admin.AlreadyBootstrapped as exc:
                print(f"error: {exc}")
                return 2
            except auth.AuthError as exc:  # password policy
                print(f"error: {exc}")
                return 2
            print(f"system-admin (password) credential {cred.id} for principal '{args.principal}'")
            print("  log in with this principal + password via the local identity provider")
            return 0
        raw, cred = admin.bootstrap_admin(args.principal)
        print(f"system-admin (token) credential {cred.id} for principal '{args.principal}'")
        print(f"  token (shown ONCE — set MNESIS_ADMIN_CREDENTIAL to it): {raw}")
        return 0

    token = os.environ.get("MNESIS_ADMIN_CREDENTIAL")
    try:
        adminp = auth.resolve_admin(token)
    except auth.AuthError as exc:
        print(f"error: admin access denied ({exc}); set MNESIS_ADMIN_CREDENTIAL to a "
              "system-admin token (mnesis admin bootstrap)")
        return 2

    if args.admin_cmd == "create-system-admin":
        password = args.password or os.environ.get("MNESIS_NEW_ADMIN_PASSWORD")
        if not password and sys.stdin.isatty():
            import getpass
            password = getpass.getpass(f"password for new system-admin '{args.principal}': ")
        if not password:
            print("error: a password is required (no default)")
            return 2
        try:
            cred = admin.create_system_admin(args.principal, password, admin=adminp)
        except auth.AuthError as exc:
            print(f"error: {exc}")
            return 2
        print(f"created system-admin credential {cred.id} for principal '{args.principal}'")
        return 0
    if args.admin_cmd == "provision":
        info = admin.provision_tenant(args.tenant_id, args.name, admin=adminp)
        print(f"provisioned tenant '{info['tenant_id']}' at {info['root']}")
        print(f"  initial admin credential {info['credential_id']}")
        print(f"  token (shown ONCE): {info['token']}")
        return 0
    if args.admin_cmd == "list":
        tenants = admin.list_tenants(admin=adminp)
        if not tenants:
            print("no tenants")
            return 0
        print("tenants:")
        for t in tenants:
            quota = f"pages<={t.max_pages}" if t.max_pages else "pages<=∞"
            print(f"  {t.tenant_id:20} {t.status:10} {quota:14} vis={t.default_visibility}")
        return 0
    if args.admin_cmd == "suspend":
        admin.suspend_tenant(args.tenant_id, admin=adminp)
        print(f"suspended tenant '{args.tenant_id}' (access denied; data retained)")
        return 0
    if args.admin_cmd == "resume":
        admin.resume_tenant(args.tenant_id, admin=adminp)
        print(f"resumed tenant '{args.tenant_id}'")
        return 0
    if args.admin_cmd == "set-quota":
        t = admin.set_quota(args.tenant_id, admin=adminp, max_pages=args.max_pages, max_bytes=args.max_bytes)
        print(f"quota for '{t.tenant_id}': max_pages={t.max_pages or '∞'} max_bytes={t.max_bytes or '∞'}")
        return 0
    if args.admin_cmd == "delete":
        try:
            res = admin.delete_tenant(args.tenant_id, admin=adminp, confirm=args.confirm)
        except admin.AdminAccessError as exc:
            print(f"error: {exc}")
            return 2
        print(f"deleted tenant '{args.tenant_id}': removed_root={res['removed_root']} "
              f"credentials_removed={res['credentials_removed']} agent_state_removed={res['agent_state_removed']}")
        return 0
    return 2


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    # Audit PDP denials for the duration of the command (never leak the global sink —
    # reset it afterwards so unrelated in-process code does no audit I/O).
    audit.enable_pdp_audit()
    try:
        return _run(args)
    finally:
        audit.disable_pdp_audit()


def _run(args: argparse.Namespace) -> int:
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

    # First-run deploy bootstrap (IAM8) — guarded/idempotent; no tenant binding.
    if args.command == "init-admin":
        password = args.password or os.environ.get("MNESIS_WEB_ADMIN_PASSWORD")
        if not password:
            print("error: a password is required (--password or MNESIS_WEB_ADMIN_PASSWORD); no default")
            return 2
        try:
            res = admin.bootstrap_tenant_admin(args.tenant, args.principal, password)
        except auth.AuthError as exc:  # password policy
            print(f"error: {exc}")
            return 2
        if res["created"]:
            print(f"created tenant-admin '{args.principal}' in tenant '{args.tenant}' — log in via the web UI")
        else:
            print(f"tenant-admin already exists in '{args.tenant}' ({res['reason']}); nothing to do")
        return 0

    # Interactive auth (IAM6): login/logout/whoami/pat operate on the credential
    # stores; they manage authentication rather than tenant data.
    if args.command == "login":
        return _cmd_login(args)
    if args.command == "logout":
        return _cmd_logout(args)
    if args.command == "whoami":
        return _cmd_whoami(args)
    if args.command == "pat":
        return _cmd_pat(args)
    if args.command == "user":
        return _cmd_user(args)

    # Credential admin operates on the credential store (outside any tenant root),
    # scoped to --tenant; admin-only when a caller is resolved.
    if args.command == "auth":
        return _cmd_auth(args)

    # System-admin tenant lifecycle (T7) — admin-only, audited; no tenant binding.
    if args.command == "admin":
        return _cmd_admin(args)

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
            # PDP enforcement (IAM6): every data command is authorized against the
            # resolved principal (role ∩ scope ∩ tenant ∩ visibility). No principal
            # bound (legacy auth-off) → permitted.
            perm = _COMMAND_PERMISSION.get(args.command)
            if perm is not None:
                try:
                    authz.require_permission(perm)
                except authz.AuthorizationError as exc:
                    who = principal.principal_id if principal else "?"
                    role = principal.role if principal else "?"
                    print(f"error: '{who}' (role {role}) is not authorized to run "
                          f"'{args.command}' ({exc.reason})")
                    return 3
            _dispatch(args)
        finally:
            if token is not None:
                auth.unbind_principal(token)
    return 0


class _CliDenied(Exception):
    """A tenant-scoped CLI op was refused (no resolved authenticated context)."""


def _collect_raw(args: argparse.Namespace) -> tuple[str | None, str]:
    """The raw credential to resolve, in precedence order, with its source label:
    the ``--token`` flag, ``MNESIS_TOKEN`` (headless PAT), the stored ``mnesis login``
    session, then the legacy ``MNESIS_CREDENTIAL``."""
    if getattr(args, "token", None):
        return args.token, "flag"
    env_token = os.environ.get("MNESIS_TOKEN")
    if env_token:
        return env_token, "env"
    stored = cli_auth.CliCredentialStore().token()
    if stored:
        return stored, "login"
    legacy = os.environ.get("MNESIS_CREDENTIAL")
    if legacy:
        return legacy, "credential"
    return None, "none"


def _resolve_optional(args: argparse.Namespace):
    """Resolve ``(TenantContext, Principal)`` from any available credential, or
    ``(None, None)`` when none is present. A **present-but-invalid** credential fails
    closed (:class:`_CliDenied`), with a re-login hint when it came from the stored
    login session."""
    raw, source = _collect_raw(args)
    if not raw:
        return None, None
    try:
        return cli_auth.resolve_token(raw)
    except auth.AuthError as exc:
        reason = getattr(exc, "reason", None) or "invalid"
        if source == "login":
            raise _CliDenied(
                f"your CLI session is {reason} — run `mnesis login` to re-authenticate"
            ) from exc
        raise _CliDenied(
            f"credential rejected ({reason}); provide a valid --token / MNESIS_TOKEN / "
            "MNESIS_CREDENTIAL"
        ) from exc


def _resolve_data_context(args: argparse.Namespace):
    """Resolve the (TenantContext, Principal|None) a tenant-scoped CLI op runs under.

    Identity comes from a verified token/credential, never the bare ``--tenant`` flag
    (see :func:`_collect_raw` for the precedence). A resolved credential's tenant is
    authoritative and any ``--tenant`` is ignored. When none is present:
      - ``MNESIS_AUTH_ENABLED`` → **refuse** (fail closed): no unauthenticated access
        to tenant data;
      - else (legacy single-tenant, auth off) → the local ``--tenant`` with no
        principal (the pre-multitenant convenience path; nothing is narrowed).
    """
    ctx, principal = _resolve_optional(args)
    if principal is not None:
        return ctx, principal
    if config.MNESIS_AUTH_ENABLED:
        raise _CliDenied(
            "not authenticated: no credential provided — run `mnesis login` (or set "
            "MNESIS_TOKEN / MNESIS_CREDENTIAL); the --tenant flag is not trusted"
        )
    return tenancy.open_tenant(args.tenant), None


if __name__ == "__main__":
    raise SystemExit(main())
