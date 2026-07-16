"""R3 — forced password change on first login (a real, enforced RESTRICTED session).

A must_change_password principal (e.g. the bootstrapped admin) authenticates, but its
session is RESTRICTED: the PDP — the ONE central enforcement point every surface reaches —
permits nothing but a change-own-password. Changing the password (verify current + policy
+ no-reuse) clears the flag and ROTATES the session to a fresh full one; the old session
dies; the same/weak password is refused; repeated bad attempts are rate-limited. Enforced
uniformly across Web, CLI, and MCP.
"""

from __future__ import annotations

import os
import stat

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from mnesis import (
    account,
    admin,
    auth,
    authz,
    cli,
    cli_auth,
    config,
    identity,
    mcp_server,
    providers,
    search,
    store,
    tenancy,
    tokens,
    webapi,
    webauth,
)
from mnesis.store import Page

PW = "correct horse battery staple"
NEW_PW = "a-brand-new-strong-passphrase-9"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """A data root whose default tenant has a bootstrapped admin (must_change_password)
    and a seeded page. Auth on; CLI creds isolated by the autouse conftest fixture."""
    monkeypatch.setattr(config, "DATA_ROOT", tmp_path / "data", raising=False)
    monkeypatch.setattr(config, "MNESIS_LLM_STUB", True, raising=False)
    monkeypatch.setattr(config, "MNESIS_AUTH_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "MNESIS_WEB_COOKIE_SECURE", False, raising=False)
    for var in ("MNESIS_TOKEN", "MNESIS_CREDENTIAL", "MNESIS_PASSWORD", "MNESIS_NEW_PASSWORD"):
        monkeypatch.delenv(var, raising=False)

    admin.bootstrap_initial_admin(username="admin", password=PW, data_root=config.DATA_ROOT)
    with tenancy.use(tenancy.open_tenant(config.DEFAULT_TENANT_ID)):
        store.write_page(Page(id="atlas", title="Atlas uses Redis", body="Atlas uses Redis."))
        search.rebuild()
    return tmp_path


# ── the bootstrapped admin is must_change_password → a restricted session ───


def test_authentication_yields_a_restricted_session(env):
    principal = providers.LocalPasswordProvider().authenticate(config.DEFAULT_TENANT_ID, "admin", PW)
    assert principal.must_change_password is True                      # on the principal
    raw, _ = tokens.TokenService().issue_session(principal)
    ap = tokens.TokenService().validate(raw)
    assert ap.must_change_password is True                             # on the issued session


# ── central PDP: the restricted session may ONLY change its password ────────


def test_pdp_restricted_session_permits_only_password_change():
    restricted = identity.Principal("admin", "acme", "admin", roles=frozenset({"admin"}),
                                    must_change_password=True)
    ctx = {"tenant_id": "acme"}
    # Everything is denied with a machine reason…
    for action in (authz.READ, authz.WRITE, authz.MAINTAIN, authz.USERS_MANAGE,
                   authz.PAGES_READ, authz.PAGES_WRITE):
        d = authz.decide(restricted, action, context=ctx)
        assert not d.allowed and d.reason == "must_change_password"
    # …except changing your own password.
    assert authz.decide(restricted, authz.PASSWORD_CHANGE, context=ctx).allowed
    # A normal principal may also change its password (self-service, any role).
    normal = identity.Principal("u", "acme", "user", roles=frozenset({"user"}))
    assert authz.decide(normal, authz.PASSWORD_CHANGE, context=ctx).allowed


# ── Web: restricted denied everywhere; change-password restores + rotates ───


def _app() -> Starlette:
    app = Starlette()
    webapi.mount_api(app)
    webauth.install(app)
    return app


def test_web_restricted_then_change_password(env):
    client = TestClient(_app())
    r = client.post("/api/auth/login", json={"username": "admin", "password": PW})
    assert r.status_code == 200
    csrf = {"X-CSRF-Token": client.cookies["mnesis_csrf"]}
    old_session = client.cookies["mnesis_session"]

    # The session reports the restriction, and every data action is denied (403).
    sess = client.get("/api/auth/session").json()
    assert sess["must_change_password"] is True and sess["permissions"] == []
    assert client.get("/api/pages").status_code == 403
    assert client.get("/api/search?q=redis").status_code == 403
    assert client.post("/api/ingest/preview", json={"text": "x", "source_ref": "s"}, headers=csrf).status_code == 403

    # A weak new password is refused; the same password is refused.
    assert client.post("/api/auth/change-password",
                       json={"current_password": PW, "new_password": "short"}, headers=csrf).status_code == 400
    assert client.post("/api/auth/change-password",
                       json={"current_password": PW, "new_password": PW}, headers=csrf).status_code == 400
    # Still restricted after the failed attempts.
    assert client.get("/api/pages").status_code == 403

    # The real change succeeds and rotates the session cookie.
    ok = client.post("/api/auth/change-password",
                     json={"current_password": PW, "new_password": NEW_PW}, headers=csrf)
    assert ok.status_code == 200 and ok.json()["must_change_password"] is False
    new_session = client.cookies["mnesis_session"]
    assert new_session != old_session

    # Normal access is restored (the new full session), and the flag is gone.
    assert client.get("/api/auth/session").json()["must_change_password"] is False
    assert client.get("/api/pages").status_code == 200

    # The OLD session no longer works (revoked on rotation).
    stale = TestClient(_app())
    stale.cookies.set("mnesis_session", old_session)
    assert stale.get("/api/pages").status_code == 401


# ── MCP: restricted denied on tools; change-password is the one allowed tool ─


def test_mcp_restricted_then_change_password(env):
    provider = providers.LocalPasswordProvider()
    principal = provider.authenticate(config.DEFAULT_TENANT_ID, "admin", PW)
    ctx = tenancy.open_tenant(config.DEFAULT_TENANT_ID)
    with tenancy.use(ctx):
        tok = auth.bind_principal(principal)
        try:
            # Every knowledge tool is denied for the restricted principal.
            for call in (lambda: mcp_server.mnesis_query("redis"),
                         lambda: mcp_server.mnesis_list(),
                         lambda: mcp_server.mnesis_get("atlas")):
                with pytest.raises(authz.AuthorizationError) as ei:
                    call()
                assert ei.value.reason == "must_change_password"
            # The one allowed tool works and clears the restriction.
            out = mcp_server.mnesis_change_password(PW, NEW_PW)
            assert "changed" in out.lower()
        finally:
            auth.unbind_principal(tok)

    # A fresh principal (re-auth with the new password) is unrestricted → tools work.
    fresh = provider.authenticate(config.DEFAULT_TENANT_ID, "admin", NEW_PW)
    assert fresh.must_change_password is False
    with tenancy.use(ctx):
        tok = auth.bind_principal(fresh)
        try:
            assert "atlas" in mcp_server.mnesis_list()
        finally:
            auth.unbind_principal(tok)


# ── CLI: restricted denied on commands; change-password restores access ─────


def _cli(capsys, *argv) -> tuple[int, str]:
    rc = cli.main(list(argv))
    return rc, capsys.readouterr().out


def test_cli_restricted_then_change_password(env, capsys):
    # Log in (stores a RESTRICTED session).
    rc, _ = _cli(capsys, "login", "--principal", "admin", "--password", PW)
    assert rc == 0

    # A data command is denied by the PDP (exit 3, must_change_password).
    rc, out = _cli(capsys, "query", "redis")
    assert rc == 3 and "must_change_password" in out

    # change-password succeeds and rotates the stored session.
    rc, out = _cli(capsys, "change-password", "--current", PW, "--new", NEW_PW)
    assert rc == 0 and "password changed" in out.lower()
    assert PW not in out and NEW_PW not in out  # never prints a secret

    # Normal access is restored under the rotated full session.
    rc, out = _cli(capsys, "query", "redis")
    assert rc == 0

    # The stored credential file holds only a token — never a password.
    text = cli_auth.CliCredentialStore().path.read_text(encoding="utf-8")
    assert PW not in text and NEW_PW not in text


# ── change-password never changes the role ──────────────────────────────────


def test_change_password_does_not_change_role(env):
    res = account.change_own_password(config.DEFAULT_TENANT_ID, "admin", PW, NEW_PW)
    assert res["principal"].role == "admin" and "admin" in res["principal"].roles
    assert res["principal"].must_change_password is False


# ── the same password and a weak password are refused ───────────────────────


def test_reuse_and_weak_password_refused(env):
    prov = providers.LocalPasswordProvider()
    with pytest.raises(providers.PasswordPolicyError):
        prov.change_password(config.DEFAULT_TENANT_ID, "admin", PW, PW)       # reuse
    with pytest.raises(providers.PasswordPolicyError):
        prov.change_password(config.DEFAULT_TENANT_ID, "admin", PW, "short")  # weak
    # Still restricted (nothing was changed).
    assert prov.authenticate(config.DEFAULT_TENANT_ID, "admin", PW).must_change_password is True


# ── repeated wrong-current-password attempts are rate-limited ───────────────


def test_change_password_is_rate_limited(env, monkeypatch):
    monkeypatch.setattr(config, "MNESIS_AUTH_MAX_FAILURES", 3, raising=False)
    prov = providers.LocalPasswordProvider()
    # Wrong current password, repeatedly → eventually locked out.
    with pytest.raises(providers.AuthenticationError):
        for _ in range(10):
            try:
                prov.change_password(config.DEFAULT_TENANT_ID, "admin", "wrong-pw", NEW_PW)
            except providers.AccountLocked:
                raise
            except providers.AuthenticationFailed:
                continue
    # A subsequent attempt (even with the correct current password) is throttled.
    with pytest.raises(providers.AccountLocked):
        prov.change_password(config.DEFAULT_TENANT_ID, "admin", PW, NEW_PW)


# ── the old session is invalidated on rotation (token level) ────────────────


def test_old_session_dies_on_rotation(env):
    principal = providers.LocalPasswordProvider().authenticate(config.DEFAULT_TENANT_ID, "admin", PW)
    svc = tokens.TokenService()
    old_raw, _ = svc.issue_session(principal)
    assert svc.validate(old_raw).must_change_password is True

    result = account.change_own_password(
        config.DEFAULT_TENANT_ID, "admin", PW, NEW_PW, session_token=old_raw
    )
    # New session is full (unrestricted); the old one is revoked.
    assert svc.validate(result["new_session"]).must_change_password is False
    with pytest.raises(identity.Deny):
        svc.validate(old_raw)
