"""Authorization & within-tenant visibility (T4, CLAUDE.md §16).

Cross-tenant access is already structurally impossible (§16, T1/T2). This is the
finer layer *inside* a resolved tenant:

  - **Authorization** — what a :class:`~mnesis.auth.Principal` may *do*. A single
    :func:`authorize` (and :func:`require`) gates reads/writes/maintenance/admin by
    role. ``admin``/``member`` may write; ``readonly`` may only read; ``agent`` gets
    a scoped set (read/write/maintain, never tenant/credential admin).
  - **Visibility** — what a principal may *see*. Pages carry ``owner_principal`` +
    ``visibility`` (``shared`` = every principal in the tenant; ``private`` =
    owner-only). The default for new pages is configurable per tenant (default
    ``shared``). Filtering is applied in the **data/query layer** (search, graph,
    get, ingest) — never only in a surface — so no surface can leak a private page.

When **no principal is bound** (the legacy single-tenant path, the CLI, internal
maintenance), nothing is narrowed: every check passes and every page is visible, so
existing single-tenant behaviour is unchanged. Enforcement engages only once a
principal is bound at a boundary (T3).
"""

from __future__ import annotations

from . import auth, config, store, tenancy, vocab

# --- Authorization ---------------------------------------------------------

#: Coarse actions the surfaces gate on.
READ = "read"
WRITE = "write"
MAINTAIN = "maintain"  # decay / rebuild / graph-lint
ADMIN = "admin"  # tenant + credential administration
ACTIONS = frozenset({READ, WRITE, MAINTAIN, ADMIN})

#: Role → capability set. ``agent`` is a non-human principal scoped to read/write/
#: maintain but never tenant/credential administration.
ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    "admin": frozenset({READ, WRITE, MAINTAIN, ADMIN}),
    "member": frozenset({READ, WRITE, MAINTAIN}),
    "agent": frozenset({READ, WRITE, MAINTAIN}),
    "readonly": frozenset({READ}),
}


class AuthorizationError(Exception):
    """A principal attempted an action its role/ownership does not permit."""


def capabilities(role: str) -> frozenset[str]:
    return ROLE_CAPABILITIES.get(role, frozenset())


def authorize(principal: "auth.Principal | None", action: str, resource=None) -> bool:
    """Return True if ``principal`` may perform ``action`` (optionally on ``resource``).

    No bound principal → permitted (legacy/CLI/internal path). Otherwise: the role
    must hold the capability for ``action``; a per-page ``read`` additionally
    requires visibility (:func:`can_see`); a per-page ``write`` on an *existing*
    page additionally requires ownership or ``admin`` (you may not mutate or
    re-scope another principal's page)."""
    if principal is None:
        return True
    if action not in capabilities(principal.role):
        return False
    if isinstance(resource, store.Page):
        if action == READ:
            return can_see(principal, resource)
        if action == WRITE:
            return _owns_or_admin(principal, resource)
    return True


def require(principal: "auth.Principal | None", action: str, resource=None) -> None:
    """:func:`authorize` or raise :class:`AuthorizationError`."""
    if not authorize(principal, action, resource):
        who = principal.principal_id if principal else "?"
        role = principal.role if principal else "?"
        raise AuthorizationError(f"{who} (role {role}) may not {action}")


def _owns_or_admin(principal: "auth.Principal", page: store.Page) -> bool:
    return principal.role == "admin" or (
        page.owner_principal is not None and page.owner_principal == principal.principal_id
    )


# --- Visibility ------------------------------------------------------------

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
