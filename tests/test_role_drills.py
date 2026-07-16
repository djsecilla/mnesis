"""R6 — the role security DRILLS, end to end (bootstrap → first-login → manage → isolate).

Exercises the whole R1–R6 stack across the CLI (`mnesis users`/`vaults`/`passwd`) and the
service, asserting every guarantee: configuration bootstrap of exactly one admin (no
default, no clobber); forced first-login password change; an admin creating an admin + a
user (each with its own tenant + default vault + forced first-login change); a new user
logging in, changing its password, and configuring its OWN vaults (entity types /
predicates); a user unable to manage accounts / self-escalate / reach another's data; the
last admin protected from demotion/deactivation; deactivation revoking access immediately;
and every action audited without secrets.
"""

from __future__ import annotations

import json

import pytest

from mnesis import (
    account,
    admin,
    authz,
    cli,
    config,
    identity,
    providers,
    store,
    tenancy,
    tokens,
    usermgmt,
)

PW = "correct horse battery staple"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    root = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_ROOT", root, raising=False)
    monkeypatch.setattr(config, "MNESIS_LLM_STUB", True, raising=False)
    monkeypatch.setattr(config, "MNESIS_AUTH_ENABLED", True, raising=False)
    for var in ("MNESIS_TOKEN", "MNESIS_CREDENTIAL", "MNESIS_PASSWORD", "MNESIS_NEW_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    return root


def _cli(capsys, *argv) -> tuple[int, str]:
    rc = cli.main(list(argv))
    return rc, capsys.readouterr().out


def _login_cli(capsys, tenant, user, pw):
    rc, _ = _cli(capsys, "--tenant", tenant, "login", "--principal", user, "--password", pw)
    assert rc == 0


def _audit(root):
    path = root / config.AUTH_AUDIT_FILENAME
    if not path.is_file():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


# ── DRILL 1: bootstrap — exactly one admin from config, no default, no clobber ──


def test_drill_bootstrap_one_admin_no_default_no_clobber(env):
    # No credential supplied → fails clearly (no default password anywhere).
    with pytest.raises(admin.BootstrapError):
        admin.bootstrap_initial_admin(username="admin", password=None, data_root=env)

    first = admin.bootstrap_initial_admin(username="admin", password=PW, data_root=env)
    assert first["created"] and first["must_change_password"] is True
    # Re-run with a different password is a NO-OP (never clobbers).
    again = admin.bootstrap_initial_admin(username="admin", password="different-pw-9", data_root=env)
    assert again["created"] is False
    assert providers.LocalPasswordProvider().authenticate(config.DEFAULT_TENANT_ID, "admin", PW)


# ── DRILL 2: forced first-login change (CLI) before anything else works ─────


def test_drill_forced_first_login_change_cli(env, capsys):
    admin.bootstrap_initial_admin(username="admin", password=PW, data_root=env)
    _login_cli(capsys, config.DEFAULT_TENANT_ID, "admin", PW)  # restricted session stored

    # Any real command is denied until the password is changed (server-enforced).
    rc, out = _cli(capsys, "users", "list")
    assert rc == 3 and "must_change_password" in out
    rc, out = _cli(capsys, "query", "anything")
    assert rc == 3 and "must_change_password" in out

    # `mnesis passwd` clears the flag and rotates the session.
    rc, out = _cli(capsys, "passwd", "--current", PW, "--new", "admin-new-passphrase-1")
    assert rc == 0 and "password changed" in out.lower()
    assert PW not in out and "admin-new-passphrase-1" not in out  # no secret printed
    rc, out = _cli(capsys, "users", "list")  # now works
    assert rc == 0


# ── DRILL 3: admin creates an admin + a user (own tenant + vault + forced change) ──


def _ready_admin_cli(env, capsys) -> None:
    admin.bootstrap_initial_admin(username="admin", password=PW, data_root=env)
    _login_cli(capsys, config.DEFAULT_TENANT_ID, "admin", PW)
    _cli(capsys, "passwd", "--current", PW, "--new", "admin-real-passphrase-1")


def test_drill_admin_creates_admin_and_user(env, capsys):
    _ready_admin_cli(env, capsys)

    rc, out = _cli(capsys, "users", "create", "--username", "carol", "--role", "user")
    assert rc == 0 and "ONE-TIME" in out
    carol_pw = out.strip().splitlines()[-1].strip()
    rc, out = _cli(capsys, "users", "create", "--username", "dave", "--role", "admin")
    assert rc == 0
    dave_pw = out.strip().splitlines()[-1].strip()

    # Each got its OWN tenant + default vault, and a forced first-login change.
    for who, pw, role in (("carol", carol_pw, "user"), ("dave", dave_pw, "admin")):
        vctx = tenancy.context_for(who, config.DEFAULT_VAULT_ID, data_root=env)
        assert vctx.root_path.exists() and vctx.pages_dir.exists()
        p = providers.LocalPasswordProvider().authenticate(who, who, pw)
        assert p.role == role and p.must_change_password is True

    # The one-time credentials never leaked into the audit.
    blob = json.dumps(_audit(env))
    assert carol_pw not in blob and dave_pw not in blob


# ── DRILL 4: new user logs in, changes password, configures its OWN vaults ──


def test_drill_user_configures_own_vaults(env, capsys):
    _ready_admin_cli(env, capsys)
    rc, out = _cli(capsys, "users", "create", "--username", "erin", "--role", "user")
    erin_pw = out.strip().splitlines()[-1].strip()

    # Erin logs in and clears the forced change.
    _cli(capsys, "logout")
    _login_cli(capsys, "erin", "erin", erin_pw)
    rc, _ = _cli(capsys, "passwd", "--current", erin_pw, "--new", "erin-real-passphrase-1")
    assert rc == 0

    # Erin creates + configures its OWN vault (entity types + predicates).
    rc, out = _cli(capsys, "--vault", "notes", "vaults", "create", "notes")
    assert rc == 0 and "created vault 'notes'" in out
    rc, out = _cli(capsys, "vaults", "config", "notes",
                   "--set-entity-types", "person,org", "--set-predicates", "employs,uses")
    assert rc == 0
    rc, out = _cli(capsys, "vaults", "config", "notes")
    assert "employs" in out and "org" in out
    rc, out = _cli(capsys, "vaults", "list")
    assert "notes" in out


# ── DRILL 5: a user cannot manage accounts / self-escalate / reach another's data ──


def test_drill_user_cannot_manage_or_escalate_or_cross(env, capsys):
    _ready_admin_cli(env, capsys)
    rc, out = _cli(capsys, "users", "create", "--username", "frank", "--role", "user")
    frank_pw = out.strip().splitlines()[-1].strip()
    _cli(capsys, "users", "create", "--username", "gina", "--role", "user")

    # Frank logs in (non-admin).
    _cli(capsys, "logout")
    _login_cli(capsys, "frank", "frank", frank_pw)
    _cli(capsys, "passwd", "--current", frank_pw, "--new", "frank-real-passphrase-1")

    # Cannot manage accounts (admin-only, PDP-enforced) …
    for sub in (("users", "list"), ("users", "create", "--username", "x", "--role", "user"),
                ("users", "deactivate", "gina"), ("users", "set-role", "gina", "admin")):
        rc, out = _cli(capsys, *sub)
        assert rc == 3 and ("insufficient_role" in out or "admin" in out.lower())

    # … cannot change its own role (PDP: not an admin at all) …
    rc, out = _cli(capsys, "users", "set-role", "frank", "admin")
    assert rc == 3

    # … and cannot reach another user's data (cross-tenant, structurally impossible).
    frank = providers.LocalPasswordProvider().authenticate("frank", "frank", "frank-real-passphrase-1")
    d = authz.decide(frank, authz.PAGES_READ, context={"tenant_id": "gina"})
    assert not d.allowed and d.reason == "cross_tenant"


# ── DRILL 6: last admin protected; deactivation is immediate ────────────────


def test_drill_last_admin_protected_and_deactivation_immediate(env):
    admin.bootstrap_initial_admin(username="admin", password=PW, data_root=env)
    account.change_own_password(config.DEFAULT_TENANT_ID, "admin", PW, "admin-real-passphrase-1")
    ada = providers.LocalPasswordProvider().authenticate(config.DEFAULT_TENANT_ID, "admin", "admin-real-passphrase-1")

    # One admin so far → cannot self-demote or self-deactivate (self + last-admin).
    with pytest.raises(usermgmt.UserManagementError):
        usermgmt.change_role(ada, "admin", "user", data_root=env)
    with pytest.raises(usermgmt.UserManagementError):
        usermgmt.deactivate_user(ada, "admin", data_root=env)

    # Create a user, give it a normal session, then deactivate → session dies at once.
    created = usermgmt.create_user(ada, "hank", "user", data_root=env)
    account.change_own_password("hank", "hank", created["initial_password"], "hank-real-passphrase-1")
    hank = providers.LocalPasswordProvider().authenticate("hank", "hank", "hank-real-passphrase-1")
    svc = tokens.TokenService()
    sess, _ = svc.issue_session(hank)
    assert svc.validate(sess)
    usermgmt.deactivate_user(ada, "hank", data_root=env)
    with pytest.raises(identity.Deny):
        svc.validate(sess)  # immediate
    # Data retained (its tenant/vault still exist).
    assert tenancy.context_for("hank", config.DEFAULT_VAULT_ID, data_root=env).root_path.exists()


# ── DRILL 7: every management action is audited without secrets (both surfaces) ──


def test_drill_actions_audited_without_secrets(env, capsys):
    _ready_admin_cli(env, capsys)
    rc, out = _cli(capsys, "users", "create", "--username", "ivy", "--role", "user")
    ivy_pw = out.strip().splitlines()[-1].strip()
    _cli(capsys, "users", "set-role", "ivy", "admin")
    _cli(capsys, "users", "set-role", "ivy", "user")  # keep a second admin around? no—ivy back to user
    _cli(capsys, "users", "reset-password", "ivy")

    events = _audit(env)
    actions = {e["event"] for e in events}
    assert {"user_created", "user_role_assigned", "user_password_reset"} <= actions
    # actor + target recorded; NO secret value or secret field anywhere.
    blob = json.dumps(events)
    assert ivy_pw not in blob
    assert not any(k in e for e in events for k in ("initial_password", "secret_hash", "password"))
    creation = [e for e in events if e["event"] == "user_created"][-1]
    assert creation["actor"] == "admin" and creation["principal_id"] == "ivy"
