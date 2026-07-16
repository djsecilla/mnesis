"""R4 — the admin-only user-management service (per-user tenancy, escalation-safe).

Every operation requires the ``admin`` role (PDP-enforced), is audited without secrets,
and is escalation-safe: no self-role-change, no last-admin lockout, deactivation
force-revokes immediately. A created user gets its OWN tenant + default vault +
must_change_password, and the creating admin can never read that user's vault DATA.
"""

from __future__ import annotations

import json

import pytest

from mnesis import (
    authz,
    config,
    identity,
    providers,
    store,
    tenancy,
    tokens,
    usermgmt,
)
from mnesis.identity import IdentityStore

PW = "correct horse battery staple"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """A data root with two admin operators (ada, bob) as per-user tenants."""
    root = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_ROOT", root, raising=False)
    monkeypatch.setattr(config, "MNESIS_LLM_STUB", True, raising=False)
    monkeypatch.setattr(config, "MNESIS_AUTH_ENABLED", True, raising=False)
    prov = providers.LocalPasswordProvider()
    for who in ("ada", "bob"):
        tenancy.create_tenant(who, data_root=root)   # per-user tenant + default vault
        prov.register(who, who, "admin", PW)
    return root


def _admin(who="ada"):
    return providers.LocalPasswordProvider().authenticate(who, who, PW)


def _audit_events(root):
    path = root / config.AUTH_AUDIT_FILENAME
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


# ── create_user: own tenant + default vault + must_change_password ──────────


def test_create_user_provisions_tenant_vault_and_one_time_credential(env):
    ada = _admin()
    res = usermgmt.create_user(ada, "carol", "user", data_root=env)
    assert res["role"] == "user" and res["must_change_password"] is True
    assert res["tenant_id"] == "carol" and res["vault_id"] == config.DEFAULT_VAULT_ID
    # The one-time credential is returned to the admin and actually works (forcing a change).
    assert res["initial_password"]
    principal = providers.LocalPasswordProvider().authenticate("carol", "carol", res["initial_password"])
    assert principal.role == "user" and principal.must_change_password is True

    # Its own tenant + default vault physically exist.
    vctx = tenancy.context_for("carol", config.DEFAULT_VAULT_ID, data_root=env)
    assert vctx.root_path.exists() and vctx.pages_dir.exists()

    # Stored hashed; the one-time password is never on disk or in the audit.
    creds_text = (env / config.CREDENTIALS_FILENAME).read_text(encoding="utf-8")
    assert res["initial_password"] not in creds_text
    assert res["initial_password"] not in (env / config.AUTH_AUDIT_FILENAME).read_text(encoding="utf-8")


def test_create_user_rejects_duplicates_and_weak_passwords(env):
    ada = _admin()
    usermgmt.create_user(ada, "carol", "user", data_root=env)
    with pytest.raises(usermgmt.UserManagementError) as ei:
        usermgmt.create_user(ada, "carol", "user", data_root=env)
    assert ei.value.reason == "exists"
    with pytest.raises(providers.PasswordPolicyError):
        usermgmt.create_user(ada, "dan", "user", password="short", data_root=env)


# ── admin-only: a non-admin is denied every operation (via the PDP) ─────────


def test_non_admin_is_denied_every_operation(env):
    # Create a plain user, then try to use it as an actor.
    ada = _admin()
    usermgmt.create_user(ada, "carol", "user", data_root=env)
    carol = identity.Principal("carol", "carol", "user", roles=frozenset({"user"}))
    for op in (
        lambda: usermgmt.create_user(carol, "x", "user", data_root=env),
        lambda: usermgmt.list_users(carol, data_root=env),
        lambda: usermgmt.change_role(carol, "bob", "user", data_root=env),
        lambda: usermgmt.deactivate_user(carol, "bob", data_root=env),
        lambda: usermgmt.reset_password(carol, "bob", data_root=env),
        lambda: usermgmt.revoke_credentials(carol, "bob", data_root=env),
    ):
        with pytest.raises(usermgmt.UserManagementError) as ei:
            op()
        assert ei.value.reason in {"insufficient_role", "no_principal"}
    # And an unauthenticated (None) actor is refused too.
    with pytest.raises(usermgmt.UserManagementError):
        usermgmt.list_users(None, data_root=env)


# ── list_users within the admin's scope (no secrets) ────────────────────────


def test_list_users_shows_accounts_without_secrets(env):
    ada = _admin()
    usermgmt.create_user(ada, "carol", "user", data_root=env)
    users = {u["username"]: u for u in usermgmt.list_users(ada, data_root=env)}
    assert set(users) == {"ada", "bob", "carol"}
    assert users["carol"]["role"] == "user" and users["carol"]["must_change_password"] is True
    assert users["ada"]["role"] == "admin" and users["ada"]["active"] is True
    assert all("password" not in u and "initial_password" not in u for u in users.values())


# ── no self-role-change ─────────────────────────────────────────────────────


def test_no_self_role_change(env):
    ada = _admin()
    with pytest.raises(usermgmt.UserManagementError) as ei:
        usermgmt.change_role(ada, "ada", "user", data_root=env)
    assert ei.value.reason == "self_role_change"


# ── no last-admin lockout (demote / deactivate / revoke) ────────────────────


def test_last_admin_cannot_be_demoted_deactivated_or_revoked(env):
    ada = _admin()
    # Two admins (ada, bob). Demote bob → ada is the sole admin.
    usermgmt.change_role(ada, "bob", "user", data_root=env)
    # ada cannot demote/deactivate/revoke itself away as the last admin…
    for op in (
        lambda: usermgmt.change_role(_admin(), "ada", "user", data_root=env),   # self-change fires first
        lambda: usermgmt.deactivate_user(_admin(), "ada", data_root=env),
        lambda: usermgmt.revoke_credentials(_admin(), "ada", data_root=env),
    ):
        with pytest.raises(usermgmt.UserManagementError):
            op()
    # A second admin makes the operation allowed again.
    usermgmt.change_role(_admin(), "bob", "admin", data_root=env)
    usermgmt.deactivate_user(_admin(), "ada", data_root=env)  # bob remains → ok


# ── deactivation force-revokes sessions/tokens immediately (retains data) ───


def test_deactivation_immediately_invalidates_sessions(env):
    ada = _admin()
    usermgmt.create_user(ada, "carol", "user", data_root=env)
    carol = providers.LocalPasswordProvider().authenticate("carol", "carol",
                                                            _reset_pw(ada, "carol", env))
    svc = tokens.TokenService()
    sess, _ = svc.issue_session(carol)
    assert svc.validate(sess)

    res = usermgmt.deactivate_user(ada, "carol", data_root=env)
    assert res["data_retained"] is True and res["tokens_revoked"] >= 1
    # The session is dead immediately, and the password no longer authenticates.
    with pytest.raises(identity.Deny):
        svc.validate(sess)
    with pytest.raises(providers.AuthenticationError):
        providers.LocalPasswordProvider().authenticate("carol", "carol", "any")
    # Data retained: the tenant + vault still exist.
    assert tenancy.context_for("carol", config.DEFAULT_VAULT_ID, data_root=env).root_path.exists()

    # Reactivate restores login with a fresh one-time credential (still must-change).
    r = usermgmt.reactivate_user(ada, "carol", data_root=env)
    back = providers.LocalPasswordProvider().authenticate("carol", "carol", r["initial_password"])
    assert back.must_change_password is True


def _reset_pw(admin, username, root) -> str:
    """Clear the created user's must_change so it can hold a normal session in a test."""
    from mnesis import account
    r = usermgmt.reset_password(admin, username, data_root=root)
    account.change_own_password(username, username, r["initial_password"], "new-strong-pass-123")
    return "new-strong-pass-123"


# ── reset_password forces a change on next login ────────────────────────────


def test_reset_password_forces_change_and_revokes_sessions(env):
    ada = _admin()
    usermgmt.create_user(ada, "carol", "user", data_root=env)
    pw = _reset_pw(ada, "carol", env)   # carol now has a normal (non-restricted) password
    carol = providers.LocalPasswordProvider().authenticate("carol", "carol", pw)
    assert carol.must_change_password is False
    svc = tokens.TokenService()
    sess, _ = svc.issue_session(carol)

    r = usermgmt.reset_password(ada, "carol", data_root=env)
    # Old session revoked; the new one-time credential forces a change on next login.
    with pytest.raises(identity.Deny):
        svc.validate(sess)
    forced = providers.LocalPasswordProvider().authenticate("carol", "carol", r["initial_password"])
    assert forced.must_change_password is True


# ── every op is audited without secrets ─────────────────────────────────────


def test_every_op_is_audited_without_secrets(env):
    ada = _admin()
    created = usermgmt.create_user(ada, "carol", "user", data_root=env)
    usermgmt.change_role(ada, "carol", "admin", data_root=env)
    reset = usermgmt.reset_password(ada, "carol", data_root=env)
    usermgmt.revoke_credentials(ada, "carol", data_root=env)

    events = _audit_events(env)
    actions = {e["event"] for e in events}
    assert {"user_created", "user_role_assigned", "user_password_reset",
            "user_credentials_revoked"} <= actions
    # Actor + target recorded; NO secret VALUE or secret-bearing field anywhere (event
    # names like "user_password_reset" are fine — we check for leaked secrets, not the word).
    blob = json.dumps(events)
    assert created["initial_password"] not in blob and reset["initial_password"] not in blob
    assert not any(k in e for e in events for k in ("initial_password", "secret_hash", "password_hash"))
    creation = [e for e in events if e["event"] == "user_created"][-1]
    assert creation["actor"] == "ada" and creation["principal_id"] == "carol"


# ── CRITICAL: creating a user never grants the admin access to its data ─────


def test_admin_cannot_read_the_new_users_vault_data(env):
    ada = _admin()
    res = usermgmt.create_user(ada, "carol", "user", data_root=env)
    # Put a page in carol's own vault.
    with tenancy.use(tenancy.context_for("carol", config.DEFAULT_VAULT_ID, data_root=env)):
        store.write_page(store.Page(id="secret", title="carol secret", body="private"))

    # The admin's tenant is credential-derived ("ada") — it can never even NAME carol's
    # tenant/vault. Acting on carol's tenant denies for ada at the PDP (cross-tenant is
    # structurally impossible); the admin role manages the account, never its data.
    d = authz.decide(ada, authz.PAGES_READ, context={"tenant_id": "carol"})
    assert not d.allowed and d.reason == "cross_tenant"
    # ada resolving a vault only ever resolves within ITS OWN tenant, never carol's.
    ada_vault = authz.resolve_vault(ada, config.DEFAULT_VAULT_ID, data_root=env)
    assert ada_vault.tenant_id == "ada"  # not carol
    with tenancy.use(ada_vault):
        with pytest.raises(FileNotFoundError):
            store.read_page("secret")  # carol's page is unreachable from ada's vault
