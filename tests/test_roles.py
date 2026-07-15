"""R1 — the two-role model (admin / user) mapped onto the single PDP.

Exactly two account roles: ``admin`` (account management + everything a ``user`` may do
in its OWN tenant/vaults) and ``user`` (own vaults/knowledge + own password). Every
permission decision flows through the existing PDP (role ∩ scope); the role-lifecycle
rules layer on top — no self-role-change, only an admin assigns roles, the last active
admin can't be demoted/deactivated, all changes audited. CRITICAL: ``admin`` is a
user-management role, NOT a data-access grant — it never widens tenant/vault isolation.
"""

from __future__ import annotations

import pytest

from mnesis import admin, authz, config, identity, providers, store, tenancy
from mnesis.authz import (
    PAGES_READ,
    PAGES_WRITE,
    ROLES_ASSIGN,
    USERS_MANAGE,
    VAULTS_CREATE,
)

PW = "correct horse battery staple"
ACME = {"tenant_id": "acme"}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Tenant ``acme`` with two admins (ada, boss) + one user (ulf); auth enabled."""
    monkeypatch.setattr(config, "DATA_ROOT", tmp_path / "data", raising=False)
    monkeypatch.setattr(config, "MNESIS_LLM_STUB", True, raising=False)
    monkeypatch.setattr(config, "MNESIS_AUTH_ENABLED", True, raising=False)
    tenancy.create_tenant("acme", data_root=config.DATA_ROOT)
    prov = providers.LocalPasswordProvider()
    prov.register("acme", "ada", "admin", PW)
    prov.register("acme", "boss", "admin", PW)
    prov.register("acme", "ulf", "user", PW)      # canonical `user` role
    prov.register("acme", "leg", "member", PW)    # legacy alias → user
    return tmp_path


def _p(tenant, user):
    return providers.LocalPasswordProvider().authenticate(tenant, user, PW)


# ── the two roles map onto the PDP matrix ───────────────────────────────────


def test_role_permission_matrix():
    # user = own data + own vaults; NO account management.
    user_perms = authz.role_permissions({"user"})
    assert {PAGES_READ, PAGES_WRITE, VAULTS_CREATE} <= user_perms
    assert USERS_MANAGE not in user_perms and ROLES_ASSIGN not in user_perms
    # member is a retained alias of user (identical perms).
    assert authz.role_permissions({"member"}) == user_perms
    assert identity.canonical_role("member") == "user"
    # admin = everything a user has + account management.
    admin_perms = authz.role_permissions({"admin"})
    assert user_perms <= admin_perms
    assert {USERS_MANAGE, ROLES_ASSIGN, authz.CREDENTIALS_ISSUE} <= admin_perms


# ── an admin may manage users; a user may not (via the PDP) ─────────────────


def test_admin_manages_users_user_cannot(env):
    ada, ulf = _p("acme", "ada"), _p("acme", "ulf")
    # PDP: admin holds users:manage/roles:assign; user holds neither.
    assert authz.decide(ada, USERS_MANAGE, context=ACME).allowed
    assert authz.decide(ada, ROLES_ASSIGN, context=ACME).allowed
    assert authz.decide(ulf, USERS_MANAGE, context=ACME).reason == "insufficient_role"
    assert authz.decide(ulf, ROLES_ASSIGN, context=ACME).reason == "insufficient_role"

    # The lifecycle functions enforce the SAME PDP.
    admin.provision_user("acme", "newbie", "user", PW, actor=ada)          # admin: ok
    with pytest.raises(admin.UserManagementError):
        admin.provision_user("acme", "sneak", "user", PW, actor=ulf)       # user: denied
    with pytest.raises(admin.UserManagementError):
        admin.set_user_role("acme", "ada", "user", actor=ulf)             # user can't assign roles


# ── a principal cannot change its own role (no self-escalation) ─────────────


def test_no_self_role_change(env):
    ada = _p("acme", "ada")
    with pytest.raises(admin.UserManagementError) as ei:
        admin.set_user_role("acme", "ada", "user", actor=ada)   # admin demoting self
    assert "own role" in str(ei.value)
    # A user cannot escalate itself to admin either (denied at the PDP first).
    ulf = _p("acme", "ulf")
    with pytest.raises(admin.UserManagementError):
        admin.set_user_role("acme", "ulf", "admin", actor=ulf)
    # ulf is still a user.
    assert "admin" not in {identity.canonical_role(r) for r in _p("acme", "ulf").roles}


# ── only an admin assigns roles; a legacy member is treated as a user ───────


def test_admin_assigns_roles_and_member_is_user(env):
    ada = _p("acme", "ada")
    # Promote the legacy `member` (leg) to admin, then back to user.
    admin.set_user_role("acme", "leg", "admin", actor=ada)
    assert "admin" in {identity.canonical_role(r) for r in _p("acme", "leg").roles}
    admin.set_user_role("acme", "leg", "member", actor=ada)   # normalised to `user`
    leg = _p("acme", "leg")
    assert leg.role == "user" and "admin" not in leg.roles


# ── the last active admin can't be demoted / deactivated (no lockout) ───────


def test_last_admin_cannot_be_demoted_or_deactivated(env):
    ada = _p("acme", "ada")
    # Two admins (ada, boss): demoting one is fine.
    admin.set_user_role("acme", "boss", "user", actor=ada)
    # Now ada is the sole admin: it cannot be demoted…
    with pytest.raises(admin.UserManagementError) as ei:
        admin.set_user_role("acme", "ada", "user", actor=_p("acme", "ada"))
    # (self-change guard fires first; use another admin to isolate the lockout rule)
    admin.set_user_role("acme", "boss", "admin", actor=ada)   # restore a second admin
    admin.set_user_role("acme", "ada", "user", actor=_p("acme", "boss"))  # now demote ada is ok
    # boss is the last admin → neither demote nor deactivate is allowed.
    with pytest.raises(admin.UserManagementError) as ei:
        admin.set_user_role("acme", "boss", "user", actor=_p("acme", "boss"))  # self anyway
    boss_creds_admin = _p("acme", "boss")
    with pytest.raises(admin.UserManagementError) as ei:
        admin.deactivate_user("acme", "boss", actor=boss_creds_admin)
    assert "last active admin" in str(ei.value)


def test_a_non_last_admin_can_be_deactivated(env):
    ada = _p("acme", "ada")
    # boss is a second admin → deactivating it is allowed (ada remains).
    admin.deactivate_user("acme", "boss", actor=ada)
    # A plain user can always be deactivated.
    admin.deactivate_user("acme", "ulf", actor=ada)


# ── CRITICAL: admin is user-management, NOT data access — isolation holds ────


def test_admin_is_not_a_data_access_grant(env):
    """An admin does not gain read/write access to another principal's vault data:
    a vault it neither owns nor is granted is refused, exactly as for a user."""
    root = config.DATA_ROOT
    ulf = _p("acme", "ulf")
    ada = _p("acme", "ada")
    # ulf creates a private vault it owns; ada (a tenant admin) is NOT granted it.
    tenancy.create_vault("acme", "ulf-vault", owner_principal="ulf", data_root=root)
    with tenancy.use(tenancy.context_for("acme", "ulf-vault", data_root=root)):
        store.write_page(store.Page(id="secret", title="ulf secret", body="private"))

    # The owner reaches it; the admin is denied (vault_forbidden) — role ≠ data access.
    assert authz.resolve_vault(ulf, "ulf-vault", data_root=root).vault_id == "ulf-vault"
    with pytest.raises(identity.Deny) as ei:
        authz.resolve_vault(ada, "ulf-vault", data_root=root)
    assert ei.value.reason == "vault_forbidden"

    # The PDP agrees: acting on the ungranted vault denies for the admin.
    ctx = {"tenant_id": "acme", "vault_id": "ulf-vault"}
    assert authz.decide(ada, PAGES_READ, context=ctx).reason == "vault_forbidden"


def test_admin_cannot_cross_the_tenant_boundary(env):
    """Isolation unchanged: an admin's role never reaches another tenant."""
    tenancy.create_tenant("beta", data_root=config.DATA_ROOT)
    providers.LocalPasswordProvider().register("beta", "badmin", "admin", PW)
    ada = _p("acme", "ada")
    # acme's admin cannot manage beta's users (tenant-match denies) …
    with pytest.raises(admin.UserManagementError):
        admin.provision_user("beta", "x", "user", PW, actor=ada)
    # … nor read beta's data.
    assert authz.decide(ada, PAGES_READ, context={"tenant_id": "beta"}).reason == "cross_tenant"


# ── role changes are audited ────────────────────────────────────────────────


def test_role_changes_are_audited(env):
    ada = _p("acme", "ada")
    admin.set_user_role("acme", "ulf", "admin", actor=ada)
    events = [e for e in providers.AuthAuditLog().all() if e.get("action") == "roles:assign"]
    assert events and events[-1]["principal_id"] == "ulf"
    assert events[-1]["actor"] == "ada" and events[-1]["role"] == "admin"
    # never a secret
    assert not any("password" in e or "token" in e for e in events)
