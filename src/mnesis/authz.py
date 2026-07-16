"""Authorization — RBAC + scopes + **the single policy decision point** (IAM4, T4, §16).

Cross-tenant access is already structurally impossible (§16, T1/T2). This module is the
**one** place any surface (Web, CLI, MCP) turns an authenticated principal into an
allow/deny decision. No surface makes ad hoc authz decisions — they all call
:func:`decide` / :func:`require_permission` / the :func:`enforce` decorator.

This module is also the **vault security core** (V2): :func:`resolve_vault` turns a
client-*selected* vault id into an *authorized* :class:`~mnesis.tenancy.VaultContext` (or
denies) **before any store is opened**, and :func:`decide` re-checks vault access on every
action. The tenant is always credential-derived and never selectable; a vault is
selectable but always re-authorized server-side against the principal's grants.

A single decision combines, **fail closed** (deny by default):

  1. **Tenant match** — the principal's tenant must equal the resource's tenant (the
     bound tenant, or an explicit ``context["tenant_id"]``); system-level actions
     (tenant lifecycle) are tenant-agnostic. Cross-tenant always denies.
  1.5 **Vault access** — the active vault (``context["vault_id"]`` else the bound
     ``VaultContext.vault_id``) must be owned or granted to the principal (the ``default``
     vault is tenant-shared); an ungranted vault denies (``vault_forbidden``).
  2. **Effective permission = role permissions ∩ credential scopes** (least privilege —
     an **intersection**, never a union). A scope entry may be a fine permission
     (``pages:write``) or a coarse class (``write``) that covers its fine permissions;
     an empty scope set means "unrestricted within the role". The role→permission
     matrix is explicit (:data:`ROLE_PERMISSIONS`).
  3. **Within-tenant visibility** (T4) — a per-page ``read`` also requires visibility; a
     per-page ``write`` also requires ownership (or ``admin``).

Every denial carries a machine ``reason`` (on the :class:`Decision` and the raised
:class:`AuthorizationError`), so it is auditable; an optional audit sink
(:func:`set_audit_sink`) is notified on every deny.

**Backward compatibility.** The T4 surface is preserved verbatim: the coarse actions
``READ``/``WRITE``/``MAINTAIN``/``ADMIN``, :func:`authorize` (bool) / :func:`require`
(raises), and every visibility helper behave exactly as before — including the
legacy "no principal bound ⇒ permitted" path for the single-tenant/CLI/internal case.
The fine-grained model and the PDP are additive.
"""

from __future__ import annotations

import contextlib
import functools
from dataclasses import dataclass

from . import auth, config, identity, store, tenancy, vocab

# --- Coarse actions (T4) — also the permission "classes" for scope families ---

READ = "read"
WRITE = "write"
MAINTAIN = "maintain"  # decay / rebuild / graph-lint
ADMIN = "admin"  # tenant + credential administration
ACTIONS = frozenset({READ, WRITE, MAINTAIN, ADMIN})
_CLASSES = ACTIONS

#: R3: the **one self-service action** every authenticated principal may always perform —
#: change its OWN password. It is NOT a role permission (any authenticated principal has
#: it, regardless of role/scope), and it is the **only** action a RESTRICTED
#: (must-change-password) session is permitted. Logout is not a PDP action (it just
#: revokes the session), so it works under restriction too.
PASSWORD_CHANGE = "password:change"


# --- Fine-grained permissions (IAM4): resource:action over the domain ---------

PAGES_READ = "pages:read"
PAGES_WRITE = "pages:write"
PAGES_DELETE = "pages:delete"  # supersede/retire a page (§12: reversible, not hard-delete)
GRAPH_MAINTAIN = "graph:maintain"  # decay / rebuild / graph-lint
AGENTS_RUN = "agents:run"  # drive the agent runtime / maintenance passes
USERS_MANAGE = "users:manage"  # manage principals within a tenant (create / deactivate / reset)
ROLES_ASSIGN = "roles:assign"  # assign/change another principal's role (R1; admin only)
CREDENTIALS_ISSUE = "credentials:issue"  # mint PATs / agent keys / credentials
EGRESS_CONFIGURE = "egress:configure"  # configure the outbound egress plane
VAULTS_CREATE = "vaults:create"  # create a new vault within one's tenant (V6)
TENANTS_MANAGE = "tenants:manage"  # provision/suspend/delete tenants (SYSTEM level)

#: Each fine permission belongs to one coarse class (used for scope-family expansion
#: and to answer the coarse T4 actions). The class is *grouping only* — what a role
#: actually grants is the explicit matrix below, never the class.
PERMISSION_CLASS: dict[str, str] = {
    PAGES_READ: READ,
    PAGES_WRITE: WRITE,
    PAGES_DELETE: WRITE,
    GRAPH_MAINTAIN: MAINTAIN,
    AGENTS_RUN: MAINTAIN,
    USERS_MANAGE: ADMIN,
    ROLES_ASSIGN: ADMIN,
    CREDENTIALS_ISSUE: ADMIN,
    EGRESS_CONFIGURE: ADMIN,
    VAULTS_CREATE: WRITE,  # a user may create their own vaults (they become the owner)
    TENANTS_MANAGE: ADMIN,
}
PERMISSIONS: frozenset[str] = frozenset(PERMISSION_CLASS)

#: Actions that are NOT tenant-scoped (they operate at the system level), so the
#: tenant-match step is skipped for them. Only the system admin's role grants these.
SYSTEM_LEVEL_ACTIONS: frozenset[str] = frozenset({TENANTS_MANAGE})


# --- The role → permission matrix (explicit) ---------------------------------
# The **two canonical account roles (R1)** are `admin` and `user`:
#   - `user`  — its OWN vaults/knowledge (pages read/write/delete, graph maintain,
#     create its own vaults) + its own password. NO account management, NO wider
#     data access.
#   - `admin` — everything `user` may do IN ITS OWN TENANT/VAULTS **plus** account
#     management: manage_users (`users:manage`: create/deactivate/reset), assign_roles
#     (`roles:assign`), issue/revoke credentials (`credentials:issue`), configure egress.
#     CRITICAL: `admin` is a USER-MANAGEMENT role, NOT a data-access grant — it holds
#     no permission that widens access to another principal's tenant or vault; the PDP's
#     tenant-match + vault-access + visibility steps gate DATA exactly as for a `user`.
# `member` is a retained ALIAS of `user` (identical perms). `agent` (machine principals)
# and `readonly` are retained specialised roles. `system_admin` is the separate SYSTEM
# boundary (tenant lifecycle), never a tenant account role.
_USER_PERMS = frozenset({PAGES_READ, PAGES_WRITE, PAGES_DELETE, GRAPH_MAINTAIN, VAULTS_CREATE})

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    # System admin operates at the system level (tenant lifecycle). It holds every
    # permission, but the tenant-match step still bars it from any single tenant's
    # data, so "manage tenants" is what it can actually do (cross-tenant page access
    # is denied by construction).
    identity.SYSTEM_ROLE: frozenset(PERMISSIONS),
    # admin = user's own-data perms + account management (never a data-access widen).
    "admin": _USER_PERMS | frozenset({USERS_MANAGE, ROLES_ASSIGN, CREDENTIALS_ISSUE, EGRESS_CONFIGURE}),
    "user": _USER_PERMS,
    "member": _USER_PERMS,  # retained alias of `user`
    "agent": frozenset({PAGES_READ, PAGES_WRITE, PAGES_DELETE, GRAPH_MAINTAIN, AGENTS_RUN}),
    "readonly": frozenset({PAGES_READ}),
}


def role_permissions(roles) -> frozenset[str]:
    """Union of the fine permissions granted by ``roles`` (unknown roles grant none)."""
    out: set[str] = set()
    for r in roles:
        out |= ROLE_PERMISSIONS.get(r, frozenset())
    return frozenset(out)


def _scope_covers(scopes: set[str], perm: str) -> bool:
    """A scope grants ``perm`` if it names the permission or its coarse class."""
    return perm in scopes or PERMISSION_CLASS.get(perm, "") in scopes


def effective_permissions(principal: "auth.Principal") -> frozenset[str]:
    """**Effective permission = role permissions ∩ credential scopes** (least privilege).

    An empty scope set means the credential is unrestricted within its roles (no
    narrowing). Otherwise only role permissions that a scope covers survive — the
    intersection, **never** the union: a scope can only ever *reduce* a role's grant."""
    rp = role_permissions(principal.roles)
    scopes = set(principal.scopes)
    if not scopes:
        return rp
    return frozenset(p for p in rp if _scope_covers(scopes, p))


#: T4 back-compat: the coarse capability classes a role covers (derived from the matrix).
ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    role: frozenset(PERMISSION_CLASS[p] for p in perms)
    for role, perms in ROLE_PERMISSIONS.items()
}


def capabilities(role: str) -> frozenset[str]:
    return ROLE_CAPABILITIES.get(role, frozenset())


# --- The decision ----------------------------------------------------------


class AuthorizationError(Exception):
    """A principal attempted an action its role/scope/ownership/tenant does not permit.
    Carries a machine ``reason`` and the full :class:`Decision`."""

    def __init__(self, message: str, *, reason: str = "denied", decision: "Decision | None" = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.decision = decision


@dataclass(frozen=True)
class Decision:
    """The PDP's verdict. ``reason`` is a stable machine code; it is set on both allow
    (``"ok"``) and deny so every outcome is auditable."""

    allowed: bool
    reason: str
    action: str = ""
    principal_id: str | None = None
    tenant_id: str | None = None

    def __bool__(self) -> bool:
        return self.allowed


# An optional sink notified on every deny (surfaces wire real auditing). Kept off by
# default so the pure decision path does no I/O.
_audit_sink = None


def set_audit_sink(sink) -> None:
    """Register ``sink(decision)`` to be called on every deny (or ``None`` to disable)."""
    global _audit_sink
    _audit_sink = sink


def _deny(reason: str, action: str, principal, *, tenant_id: str | None = None) -> Decision:
    d = Decision(
        allowed=False,
        reason=reason,
        action=action,
        principal_id=getattr(principal, "principal_id", None),
        tenant_id=tenant_id if tenant_id is not None else getattr(principal, "tenant_id", None),
    )
    if _audit_sink is not None:
        try:
            _audit_sink(d)
        except Exception:
            pass
    return d


def _target_tenant(principal, context) -> str | None:
    if context and context.get("tenant_id"):
        return context["tenant_id"]
    ctx = tenancy.current_or_none()
    return ctx.tenant_id if ctx is not None else None


def _target_vault(principal, context) -> str | None:
    """The vault the action targets: an explicit ``context["vault_id"]`` else the bound
    :class:`~mnesis.tenancy.VaultContext`'s ``vault_id`` (``None`` when no vault is in
    play, e.g. a pure-permission decision with nothing bound)."""
    if context and context.get("vault_id"):
        return context["vault_id"]
    ctx = tenancy.current_or_none()
    return getattr(ctx, "vault_id", None) if ctx is not None else None


def _vault_authorized(principal: "auth.Principal", vault_id: str, *, data_root=None) -> bool:
    """Whether ``principal`` may access ``vault_id`` within **its own** tenant — the
    server-side re-authorization of a (selectable) vault. The transparent ``default``
    vault is accessible to every tenant member (single-vault deployments); a named vault
    must exist in this tenant AND be owned-or-granted. **Fail closed** — any lookup error,
    an unknown vault, or a missing grant denies."""
    if vault_id == config.DEFAULT_VAULT_ID:
        return True
    try:
        reg = tenancy.tenant_context_for(principal.tenant_id, data_root=data_root).vault_registry()
        if not reg.exists(vault_id):
            return False
        return reg.has_access(principal.principal_id, vault_id)
    except Exception:
        return False


def decide(principal: "auth.Principal | None", action: str, resource=None, context: dict | None = None) -> Decision:
    """**The PDP.** Return an allow/deny :class:`Decision`, fail closed.

    Combines tenant match, effective permission (role ∩ scope), and within-tenant
    visibility. ``action`` may be a fine permission (``pages:write``) or a coarse class
    (``read``/``write``/``maintain``/``admin``). A missing principal denies (a pure
    fail-closed decision — the lenient legacy path lives in :func:`authorize`/:func:`require`)."""
    if principal is None:
        return _deny("no_principal", action, principal)

    # R3 — the restricted-session gate (central; every surface reaches it via the PDP):
    #   * change-own-password is a self-service action ALWAYS permitted to an authenticated
    #     principal (no role/scope/tenant/vault checks — you are changing your OWN secret);
    #   * a principal in the must_change_password state gets a RESTRICTED session — every
    #     other action is denied until it rotates its password.
    if action == PASSWORD_CHANGE:
        return Decision(True, "ok", action, principal.principal_id, principal.tenant_id)
    if getattr(principal, "must_change_password", False):
        return _deny("must_change_password", action, principal)

    fine = action if action in PERMISSIONS else None
    if fine is None and action not in _CLASSES:
        return _deny("unknown_action", action, principal)

    # 1) Tenant match (skipped for system-level actions).
    if action not in SYSTEM_LEVEL_ACTIONS:
        target = _target_tenant(principal, context)
        if target is not None and principal.tenant_id != target:
            return _deny("cross_tenant", action, principal, tenant_id=target)

    # 1.5) Vault access — the active vault is SELECTABLE but must be authorized against
    # the principal's grants (V2). Defense in depth alongside `resolve_vault` at the
    # boundary: even if a store is somehow bound, an action on an ungranted vault denies.
    if action not in SYSTEM_LEVEL_ACTIONS:
        vault_id = _target_vault(principal, context)
        if vault_id is not None and not _vault_authorized(principal, vault_id):
            return _deny("vault_forbidden", action, principal)

    # 2) Effective permission = role ∩ scope.
    rp = role_permissions(principal.roles)
    eff = effective_permissions(principal)
    if fine is not None:
        if fine not in eff:
            reason = "out_of_scope" if fine in rp else "insufficient_role"
            return _deny(reason, action, principal)
    else:  # coarse class: satisfied by ANY permission of that class
        class_perms = {p for p, c in PERMISSION_CLASS.items() if c == action}
        if not (eff & class_perms):
            reason = "out_of_scope" if (rp & class_perms) else "insufficient_role"
            return _deny(reason, action, principal)

    # 3) Within-tenant visibility / ownership for a concrete page.
    if isinstance(resource, store.Page):
        is_read = action == READ or fine == PAGES_READ
        is_write = action == WRITE or fine in (PAGES_WRITE, PAGES_DELETE)
        if is_read and not can_see(principal, resource):
            return _deny("not_visible", action, principal)
        if is_write and not _owns_or_admin(principal, resource):
            return _deny("not_owner", action, principal)

    return Decision(True, "ok", action, getattr(principal, "principal_id", None), getattr(principal, "tenant_id", None))


# --- Enforcement API (the one call surfaces make) --------------------------


def authorize(principal: "auth.Principal | None", action: str, resource=None, context: dict | None = None) -> bool:
    """Boolean gate. **Legacy-compatible:** an unbound principal (``None``) is permitted
    (single-tenant/CLI/internal path). A bound principal goes through the full PDP."""
    if principal is None:
        return True
    return decide(principal, action, resource, context).allowed


def require(principal: "auth.Principal | None", action: str, resource=None, context: dict | None = None) -> None:
    """:func:`authorize` or raise :class:`AuthorizationError` (with the deny reason).
    An unbound principal is permitted (legacy path), matching T4."""
    if principal is None:
        return
    d = decide(principal, action, resource, context)
    if not d.allowed:
        who = d.principal_id or "?"
        raise AuthorizationError(f"{who} may not {action} ({d.reason})", reason=d.reason, decision=d)


def require_permission(action: str, resource=None, context: dict | None = None) -> None:
    """The **surface entry point**: enforce ``action`` for the currently-bound principal
    in one call. **Fail closed** — when auth is enabled and no principal is bound, deny;
    when auth is off (legacy single-tenant), permit (nothing to narrow)."""
    principal = auth.current_principal_or_none()
    if principal is None:
        if config.MNESIS_AUTH_ENABLED:
            d = _deny("no_principal", action, None)
            raise AuthorizationError("no authenticated principal", reason="no_principal", decision=d)
        return
    require(principal, action, resource, context)


def enforce(action: str):
    """Decorator: gate a handler/tool with a single line — ``@enforce(authz.PAGES_WRITE)``.
    Enforces ``action`` for the bound principal before the wrapped function runs."""

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            require_permission(action)
            return fn(*args, **kwargs)

        return wrapper

    return deco


# --- Vault resolution & access control (V2, the security core) -------------


def resolve_vault(
    principal: "auth.Principal | None",
    requested_vault_id: str | None = None,
    *,
    data_root=None,
) -> "tenancy.VaultContext":
    """Resolve a client-**selected** vault to an **authorized** :class:`VaultContext`, or
    raise :class:`identity.Deny` (fail closed).

    The one place vaults differ from tenants: ``requested_vault_id`` MAY come from the
    client (a selection), but access is re-authorized server-side **before any store is
    opened** — validated against (1) the vault's tenant matching the principal's
    credential-derived tenant (a vault from another tenant is always denied), and (2) the
    principal's grants (ownership or an explicit grant; the transparent ``default`` vault
    is shared within the tenant). Denies — with no default-to-some-vault fallback — when
    the principal is unauthenticated, or the vault is malformed, unknown, cross-tenant,
    inactive, or not granted. The returned context is a pure path handle (no store, no
    side effects); the caller binds it and opens stores only after this succeeds."""
    if principal is None or not getattr(principal, "tenant_id", None):
        raise identity.Deny("no authenticated principal for vault resolution", reason="no_principal")
    tenant_id = principal.tenant_id
    vault_id = config.DEFAULT_VAULT_ID if requested_vault_id is None else requested_vault_id

    # A crafted id (traversal / separator / absolute) can never even name a vault.
    try:
        tenancy.validate_vault_id(vault_id)
    except tenancy.InvalidVaultId as exc:
        raise identity.Deny(f"invalid vault id {vault_id!r}", reason="invalid_vault") from exc

    tctx = tenancy.tenant_context_for(tenant_id, data_root=data_root)
    reg = tctx.vault_registry()

    if vault_id != config.DEFAULT_VAULT_ID:
        vault = reg.get(vault_id)
        if vault is None:
            # Not in THIS tenant's registry — unknown here (a same-named vault under
            # another tenant is invisible; tenant is never selectable).
            raise identity.Deny(f"unknown vault {vault_id!r}", reason="unknown_vault")
        if vault.tenant_id != tenant_id:  # defensive: the registry is per-tenant already
            raise identity.Deny("cross-tenant vault access", reason="cross_tenant")
        if vault.status != "active":
            raise identity.Deny(f"vault {vault_id!r} is not active", reason="vault_inactive")
        if not reg.has_access(principal.principal_id, vault_id):
            raise identity.Deny(
                f"{principal.principal_id} is not granted vault {vault_id!r}", reason="vault_forbidden"
            )

    # AUTHORIZED — only now build the (store-less) context handle for the selected vault.
    return tctx.vault_context(vault_id)


def open_authorized_vault(
    principal: "auth.Principal | None",
    requested_vault_id: str | None = None,
    *,
    data_root=None,
) -> "tenancy.VaultContext":
    """:func:`resolve_vault` (authorize) **then** ensure the vault is provisioned/usable —
    the single 'select → authorize → open' step every surface's choke point runs. The
    ``default`` vault runs the transparent provisioning/migration (`tenancy.open_tenant`);
    a granted named vault is already provisioned, so its authorized handle is returned as
    is. Raises :class:`identity.Deny` (fail closed) on any unauthorized selection — no
    store is opened before authorization succeeds."""
    ctx = resolve_vault(principal, requested_vault_id, data_root=data_root)
    if ctx.vault_id == config.DEFAULT_VAULT_ID:
        return tenancy.open_tenant(ctx.tenant_id, data_root=data_root)
    return ctx


@contextlib.contextmanager
def use_vault(
    principal: "auth.Principal | None", requested_vault_id: str | None = None, *, data_root=None
):
    """Bind the AUTHORIZED vault for the duration of a block (the surface primitive)."""
    ctx = open_authorized_vault(principal, requested_vault_id, data_root=data_root)
    with tenancy.use(ctx):
        yield ctx


@contextlib.contextmanager
def authenticated_vault(
    credential: str | None,
    requested_vault_id: str | None = None,
    *,
    cred_store=None,
    data_root=None,
):
    """Resolve ``credential`` → principal, authorize + open the SELECTED vault, and bind
    BOTH (vault + principal) for the block — exactly what the MCP/CLI choke points do.
    Fail closed: an unresolved credential or an unauthorized vault raises
    :class:`identity.Deny`."""
    _, principal = auth.resolve_principal(credential, store=cred_store, data_root=data_root)
    ctx = open_authorized_vault(principal, requested_vault_id, data_root=data_root)
    with tenancy.use(ctx):
        tok = auth.bind_principal(principal)
        try:
            yield ctx, principal
        finally:
            auth.unbind_principal(tok)


def vault_role(
    principal: "auth.Principal", vault_id: str, *, data_root=None
) -> str:
    """The principal's effective role **in** ``vault_id`` — an explicit per-vault grant
    role if set, else the principal's tenant role (the default). For the ``default`` vault
    (or any vault with no explicit grant role) this is the tenant role."""
    default_role = getattr(principal, "role", "readonly")
    if vault_id == config.DEFAULT_VAULT_ID:
        return default_role
    try:
        reg = tenancy.tenant_context_for(principal.tenant_id, data_root=data_root).vault_registry()
        return reg.role_for(principal.principal_id, vault_id, default_role)
    except Exception:
        return default_role


def grant_vault_access(
    tenant_id: str, principal_id: str, vault_id: str, role: str | None = None, *, data_root=None
) -> None:
    """Grant ``principal_id`` access to ``vault_id`` within ``tenant_id`` (idempotent;
    ``role`` optionally sets a per-vault role). Admin-gated at the surface; the vault must
    exist in the tenant's registry."""
    tenancy.tenant_context_for(tenant_id, data_root=data_root).vault_registry().grant(
        principal_id, vault_id, role
    )


def revoke_vault_access(
    tenant_id: str, principal_id: str, vault_id: str, *, data_root=None
) -> bool:
    """Revoke ``principal_id``'s explicit grant to ``vault_id`` (ownership is unaffected)."""
    return tenancy.tenant_context_for(tenant_id, data_root=data_root).vault_registry().revoke_grant(
        principal_id, vault_id
    )


def accessible_vaults(principal: "auth.Principal", *, data_root=None) -> set[str]:
    """The vault ids ``principal`` may reach in its tenant = owned ∪ granted ∪ the
    transparent ``default`` vault."""
    try:
        reg = tenancy.tenant_context_for(principal.tenant_id, data_root=data_root).vault_registry()
        return reg.accessible_vaults(principal.principal_id) | {config.DEFAULT_VAULT_ID}
    except Exception:
        return {config.DEFAULT_VAULT_ID}


def _owns_or_admin(principal: "auth.Principal", page: store.Page) -> bool:
    return principal.role == "admin" or principal.role == identity.SYSTEM_ROLE or (
        page.owner_principal is not None and page.owner_principal == principal.principal_id
    )


# --- Visibility (T4) — unchanged ------------------------------------------

PRIVATE = "private"
SHARED = "shared"
VISIBILITIES = frozenset({PRIVATE, SHARED})


def normalize_visibility(value: str | None) -> str:
    v = (value or "").strip().lower()
    return v if v in VISIBILITIES else SHARED


def default_visibility(ctx: "tenancy.TenantContext | None" = None) -> str:
    """The default visibility for a new page in a tenant: the tenant's own setting
    (registry) if any, else the global ``MNESIS_DEFAULT_VISIBILITY`` (else shared)."""
    ctx = ctx if ctx is not None else tenancy.current_or_none()
    if ctx is not None:
        tenant = tenancy.TenantRegistry().get(ctx.tenant_id)
        if tenant is not None:
            return normalize_visibility(tenant.default_visibility)
    return normalize_visibility(config.MNESIS_DEFAULT_VISIBILITY)


def can_see(principal: "auth.Principal | None", page: store.Page) -> bool:
    """Whether ``principal`` may see ``page`` within the (already-resolved) tenant.

    ``shared`` pages are visible to all principals in the tenant; ``private`` pages
    only to their owner (and to an ``admin``, for governance). An unowned page
    (legacy/no-owner) is treated as shared. No principal bound → visible."""
    if principal is None:
        return True
    if normalize_visibility(page.visibility) == SHARED or page.owner_principal is None:
        return True
    return page.owner_principal == principal.principal_id or principal.role == "admin"


# -- the principal-aware filters used by the data/query layer ---------------


def visible_pages(principal: "auth.Principal") -> list[store.Page]:
    """The pages in the active tenant ``principal`` may see."""
    return [p for p in store.list_pages() if can_see(principal, p)]


def visible_page_ids(principal: "auth.Principal") -> set[str]:
    return {p.id for p in visible_pages(principal)}


def _page_entity_refs(page: store.Page) -> set[str]:
    """Every graph node a page contributes (mirrors graph.py's projection): its own
    page node, its entity-typed tags, its relation endpoints, and the page nodes its
    supersedes/contradicts links touch."""
    refs: set[str] = {f"page:{page.id}"}
    for tag in page.tags:
        try:
            refs.add(vocab.normalize_ref(tag))
        except ValueError:
            continue
    for rel in page.relations:
        if {"s", "o"} <= rel.keys():
            refs.add(rel["s"])
            refs.add(rel["o"])
    if page.supersedes:
        refs.add(f"page:{page.supersedes}")
    for other in page.contradicts:
        refs.add(f"page:{other}")
    return refs


def visible_entity_refs(principal: "auth.Principal") -> set[str]:
    """Every graph node backed by at least one page ``principal`` may see."""
    refs: set[str] = set()
    for page in visible_pages(principal):
        refs |= _page_entity_refs(page)
    return refs


# -- "active principal" convenience (None when nobody is bound) --------------


def active_visible_page_ids() -> set[str] | None:
    """Visible page ids for the bound principal, or ``None`` (no filtering)."""
    p = auth.current_principal_or_none()
    return None if p is None else visible_page_ids(p)


def active_visible_entity_refs() -> set[str] | None:
    p = auth.current_principal_or_none()
    return None if p is None else visible_entity_refs(p)


def page_visible_to_active(page: store.Page) -> bool:
    """Whether the bound principal (if any) may see ``page``."""
    return can_see(auth.current_principal_or_none(), page)
