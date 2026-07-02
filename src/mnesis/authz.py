"""Authorization — RBAC + scopes + **the single policy decision point** (IAM4, T4, §16).

Cross-tenant access is already structurally impossible (§16, T1/T2). This module is the
**one** place any surface (Web, CLI, MCP) turns an authenticated principal into an
allow/deny decision. No surface makes ad hoc authz decisions — they all call
:func:`decide` / :func:`require_permission` / the :func:`enforce` decorator.

A single decision combines, **fail closed** (deny by default):

  1. **Tenant match** — the principal's tenant must equal the resource's tenant (the
     bound tenant, or an explicit ``context["tenant_id"]``); system-level actions
     (tenant lifecycle) are tenant-agnostic. Cross-tenant always denies.
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


# --- Fine-grained permissions (IAM4): resource:action over the domain ---------

PAGES_READ = "pages:read"
PAGES_WRITE = "pages:write"
PAGES_DELETE = "pages:delete"  # supersede/retire a page (§12: reversible, not hard-delete)
GRAPH_MAINTAIN = "graph:maintain"  # decay / rebuild / graph-lint
AGENTS_RUN = "agents:run"  # drive the agent runtime / maintenance passes
USERS_MANAGE = "users:manage"  # manage principals within a tenant
CREDENTIALS_ISSUE = "credentials:issue"  # mint PATs / agent keys / credentials
EGRESS_CONFIGURE = "egress:configure"  # configure the outbound egress plane
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
    CREDENTIALS_ISSUE: ADMIN,
    EGRESS_CONFIGURE: ADMIN,
    TENANTS_MANAGE: ADMIN,
}
PERMISSIONS: frozenset[str] = frozenset(PERMISSION_CLASS)

#: Actions that are NOT tenant-scoped (they operate at the system level), so the
#: tenant-match step is skipped for them. Only the system admin's role grants these.
SYSTEM_LEVEL_ACTIONS: frozenset[str] = frozenset({TENANTS_MANAGE})


# --- The role → permission matrix (explicit) ---------------------------------
# Roles: system_admin, admin (tenant-admin), member, readonly, agent. `agent` is a
# non-human principal — read/write/maintain + run, never tenant/credential/user admin.

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    # System admin operates at the system level (tenant lifecycle). It holds every
    # permission, but the tenant-match step still bars it from any single tenant's
    # data, so "manage tenants" is what it can actually do (cross-tenant page access
    # is denied by construction).
    identity.SYSTEM_ROLE: frozenset(PERMISSIONS),
    "admin": frozenset({
        PAGES_READ, PAGES_WRITE, PAGES_DELETE, GRAPH_MAINTAIN, AGENTS_RUN,
        USERS_MANAGE, CREDENTIALS_ISSUE, EGRESS_CONFIGURE,
    }),
    "member": frozenset({PAGES_READ, PAGES_WRITE, PAGES_DELETE, GRAPH_MAINTAIN}),
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


def decide(principal: "auth.Principal | None", action: str, resource=None, context: dict | None = None) -> Decision:
    """**The PDP.** Return an allow/deny :class:`Decision`, fail closed.

    Combines tenant match, effective permission (role ∩ scope), and within-tenant
    visibility. ``action`` may be a fine permission (``pages:write``) or a coarse class
    (``read``/``write``/``maintain``/``admin``). A missing principal denies (a pure
    fail-closed decision — the lenient legacy path lives in :func:`authorize`/:func:`require`)."""
    if principal is None:
        return _deny("no_principal", action, principal)

    fine = action if action in PERMISSIONS else None
    if fine is None and action not in _CLASSES:
        return _deny("unknown_action", action, principal)

    # 1) Tenant match (skipped for system-level actions).
    if action not in SYSTEM_LEVEL_ACTIONS:
        target = _target_tenant(principal, context)
        if target is not None and principal.tenant_id != target:
            return _deny("cross_tenant", action, principal, tenant_id=target)

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
