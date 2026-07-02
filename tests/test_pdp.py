"""IAM4 — RBAC + scopes + the single policy decision point (`authz.decide`).

One PDP combines tenant match, effective permission (**role ∩ scope**, least
privilege), and within-tenant visibility, failing closed with an auditable reason.
Members write but can't manage users; readonly can't write; a scope-limited token
can't exceed its scope even when its role would allow it; a system-admin manages
tenants; every cross-tenant action denies.
"""

from __future__ import annotations

import pytest

from mnesis import authz, identity
from mnesis.authz import (
    CREDENTIALS_ISSUE,
    PAGES_DELETE,
    PAGES_READ,
    PAGES_WRITE,
    TENANTS_MANAGE,
    USERS_MANAGE,
)
from mnesis.store import Page


def member(scopes=()):
    return identity.Principal("alice", "acme", "member", roles=frozenset({"member"}), scopes=frozenset(scopes))


def readonly():
    return identity.Principal("rob", "acme", "readonly", roles=frozenset({"readonly"}))


def tenant_admin():
    return identity.Principal("ada", "acme", "admin", roles=frozenset({"admin"}))


def agent(scopes=()):
    return identity.Principal("bot", "acme", "agent", roles=frozenset({"agent"}), scopes=frozenset(scopes))


def system_admin():
    return identity.Principal("root", identity.SYSTEM_TENANT, identity.SYSTEM_ROLE,
                              roles=frozenset({identity.SYSTEM_ROLE}))


ACME = {"tenant_id": "acme"}


# ── member: write within tenant/visibility, but not manage users ────────────


def test_member_can_write_within_tenant(pdp_free):
    d = authz.decide(member(), PAGES_WRITE, context=ACME)
    assert d.allowed and d.reason == "ok"
    assert authz.decide(member(), authz.WRITE, context=ACME).allowed  # coarse form too


def test_member_cannot_manage_users(pdp_free):
    d = authz.decide(member(), USERS_MANAGE, context=ACME)
    assert not d.allowed and d.reason == "insufficient_role"


def test_member_write_respects_visibility_and_ownership(pdp_free):
    mine = Page(id="p1", title="mine", body="x", owner_principal="alice", visibility="private")
    theirs = Page(id="p2", title="theirs", body="x", owner_principal="bob", visibility="private")
    # Owner reads/writes their private page; a non-owner can't see or write it.
    assert authz.decide(member(), PAGES_READ, mine, context=ACME).allowed
    assert authz.decide(member(), PAGES_WRITE, mine, context=ACME).allowed
    assert authz.decide(member(), PAGES_READ, theirs, context=ACME).reason == "not_visible"
    assert authz.decide(member(), PAGES_WRITE, theirs, context=ACME).reason == "not_owner"
    # A tenant-admin overrides ownership (governance).
    assert authz.decide(tenant_admin(), PAGES_WRITE, theirs, context=ACME).allowed


# ── readonly cannot write ───────────────────────────────────────────────────


def test_readonly_cannot_write(pdp_free):
    assert authz.decide(readonly(), PAGES_READ, context=ACME).allowed
    d = authz.decide(readonly(), PAGES_WRITE, context=ACME)
    assert not d.allowed and d.reason == "insufficient_role"


# ── scopes narrow: intersection, never union ────────────────────────────────


def test_scope_limited_token_cannot_exceed_scope(pdp_free):
    # A member (role allows write) but the credential is scoped read-only.
    scoped = member(scopes={PAGES_READ})
    assert authz.decide(scoped, PAGES_READ, context=ACME).allowed
    d = authz.decide(scoped, PAGES_WRITE, context=ACME)
    # Role WOULD allow it, so the deny reason is scope, not role.
    assert not d.allowed and d.reason == "out_of_scope"


def test_coarse_scope_family_narrows_but_stays_within_role(pdp_free):
    # A coarse "read" scope covers pages:read only; a coarse "admin" scope grants
    # nothing extra to a member (intersection with the role's perms is empty of admin).
    r_scoped = member(scopes={authz.READ})
    assert authz.decide(r_scoped, PAGES_READ, context=ACME).allowed
    assert authz.decide(r_scoped, PAGES_WRITE, context=ACME).reason == "out_of_scope"
    admin_scoped_member = member(scopes={authz.ADMIN})
    assert authz.decide(admin_scoped_member, USERS_MANAGE, context=ACME).reason == "insufficient_role"
    # The scope only ever REDUCES: effective ⊆ role permissions.
    eff = authz.effective_permissions(admin_scoped_member)
    assert eff <= authz.role_permissions({"member"})


def test_effective_is_intersection_helper(pdp_free):
    p = member(scopes={PAGES_READ, USERS_MANAGE})  # USERS_MANAGE not in member's role
    # Intersection drops the scope the role never had — not a union.
    assert authz.effective_permissions(p) == frozenset({PAGES_READ})


# ── system-admin manages tenants ────────────────────────────────────────────


def test_system_admin_can_manage_tenants(pdp_free):
    assert authz.decide(system_admin(), TENANTS_MANAGE).allowed
    # A tenant-admin cannot (the role simply lacks it).
    d = authz.decide(tenant_admin(), TENANTS_MANAGE, context=ACME)
    assert not d.allowed and d.reason == "insufficient_role"


def test_system_admin_cannot_touch_a_tenants_pages(pdp_free):
    # System admin holds the permission but tenant-match bars cross-tenant data access.
    d = authz.decide(system_admin(), PAGES_WRITE, context=ACME)
    assert not d.allowed and d.reason == "cross_tenant"


# ── every cross-tenant action denies ────────────────────────────────────────


def test_cross_tenant_denies(pdp_free):
    other = {"tenant_id": "other-corp"}
    for principal, action in [
        (member(), PAGES_READ), (member(), PAGES_WRITE),
        (tenant_admin(), USERS_MANAGE), (agent(), PAGES_DELETE),
    ]:
        d = authz.decide(principal, action, context=other)
        assert not d.allowed and d.reason == "cross_tenant"


# ── fail closed + denials carry a reason + auditable ────────────────────────


def test_missing_principal_and_unknown_action_deny(pdp_free):
    assert authz.decide(None, PAGES_READ).reason == "no_principal"
    assert authz.decide(member(), "pages:teleport", context=ACME).reason == "unknown_action"


def test_denials_carry_a_reason_and_hit_the_audit_sink(pdp_free):
    seen = []
    authz.set_audit_sink(seen.append)
    try:
        with pytest.raises(authz.AuthorizationError) as ei:
            authz.require(readonly(), PAGES_WRITE, context=ACME)
        assert ei.value.reason == "insufficient_role"
        assert ei.value.decision is not None and ei.value.decision.action == PAGES_WRITE
    finally:
        authz.set_audit_sink(None)
    # The deny was surfaced to the audit sink with its reason (auditable).
    assert seen and seen[-1].reason == "insufficient_role" and seen[-1].allowed is False


# ── agent: scoped, never admin ──────────────────────────────────────────────


def test_agent_runs_but_never_administers(pdp_free):
    assert authz.decide(agent(), authz.AGENTS_RUN, context=ACME).allowed
    assert authz.decide(agent(), CREDENTIALS_ISSUE, context=ACME).reason == "insufficient_role"


@pytest.fixture()
def pdp_free():
    """The PDP is a pure function; these tests bind no tenant (tenant match uses the
    explicit context). Ensure no ambient tenant leaks in from another test."""
    from mnesis import tenancy

    tok = None
    ctx = tenancy.current_or_none()
    if ctx is not None:  # pragma: no cover - defensive
        pytest.skip("a tenant is unexpectedly bound")
    yield tok
