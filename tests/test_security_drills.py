"""IAM8 — the security DRILLS, end to end.

One suite that walks the whole identity stack the way an auditor would:
  - unauthenticated access is refused on web / cli / mcp;
  - each credential type authenticates and scopes correctly;
  - a scope/role can't be exceeded;
  - revocation is immediate everywhere;
  - a tenant-admin can't touch another tenant;
  - deactivation force-revokes a user's credentials + tokens;
  - the injected token no longer authenticates;
  - auth events are audited without secrets;
  - bootstrap creates the first admin with no default password.
"""

from __future__ import annotations

import json
import stat

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from mnesis import (
    admin, audit, auth, authz, cli, cli_auth, config, identity, mcp_server,
    providers, search, store, tenancy, tokens, webapi, webauth,
)
from mnesis.store import Page

PW = "correct horse battery staple"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """A fresh data root; two tenants (acme, beta) each with an admin + a member,
    seeded pages, and the local password provider. Auth enabled."""
    monkeypatch.setattr(config, "DATA_ROOT", tmp_path / "data", raising=False)
    monkeypatch.setattr(config, "MNESIS_LLM_STUB", True, raising=False)
    monkeypatch.setattr(config, "MNESIS_AUTH_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "MNESIS_WEB_COOKIE_SECURE", False, raising=False)
    monkeypatch.delenv("MNESIS_TOKEN", raising=False)
    monkeypatch.delenv("MNESIS_CREDENTIAL", raising=False)

    prov = providers.LocalPasswordProvider()
    for t in ("acme", "beta"):
        tenancy.create_tenant(t, data_root=config.DATA_ROOT)
        with tenancy.use(tenancy.context_for(t, data_root=config.DATA_ROOT)):
            store.write_page(Page(id=f"{t}-fact", title=f"{t} uses Redis", body=f"{t} uses Redis."))
            search.rebuild()
        prov.register(t, "adm", "admin", PW)
        prov.register(t, "mem", "member", PW)
    return tmp_path


def _principal(tenant, user):
    return providers.LocalPasswordProvider().authenticate(tenant, user, PW)


# ── DRILL 1: unauthenticated access is refused on web / cli / mcp ──────────


def test_drill_unauthenticated_refused_everywhere(env, capsys):
    # web
    app = Starlette()
    webapi.mount_api(app)
    webauth.install(app)
    with TestClient(app) as c:
        assert c.get("/api/pages").status_code == 401
    # cli (auth enabled, no credential)
    rc = cli.main(["query", "redis"])
    assert rc == 2 and "not authenticated" in capsys.readouterr().out.lower()
    # mcp middleware (bare request → 401 via resolve_bearer)
    with pytest.raises(identity.Deny):
        tokens.resolve_bearer(None)


# ── DRILL 2 + 3: each credential type authenticates + scopes; can't exceed ──


def test_drill_credential_types_scope_and_cannot_exceed(env):
    svc = tokens.TokenService()
    member = _principal("acme", "mem")
    # session (web login)
    s_raw, _ = svc.issue_session(member)
    assert svc.validate(s_raw).principal_id == "mem"
    # PAT (headless), scoped read-only — cannot exceed even though member could write
    p_raw, _ = svc.issue_pat(member, "ci", [authz.READ])
    with _bound(p_raw):
        mcp_server.mnesis_list()                       # read allowed
        with pytest.raises(authz.AuthorizationError):
            mcp_server.mnesis_ingest("x", "s")         # write out of scope
    # agent key (mcp), least privilege per kind
    a_raw, _ = tokens.issue_agent_key_for("writing", "acme", "bot", service=svc)
    with _bound(a_raw):
        assert "ingested" in mcp_server.mnesis_ingest("Redis note.", "s2")  # write allowed
        with pytest.raises(authz.AuthorizationError):
            mcp_server.mnesis_query("redis")           # read out of scope


# ── DRILL 4: revocation is immediate everywhere ────────────────────────────


def test_drill_revocation_is_immediate(env):
    svc = tokens.TokenService()
    raw, rec = svc.issue_pat(_principal("acme", "mem"), "ci", [authz.READ])
    assert svc.validate(raw).principal_id == "mem"
    svc.revoke(rec.id)
    with pytest.raises(identity.Deny):
        svc.validate(raw)                              # next call denies — no delay


# ── DRILL 5: a tenant-admin can't touch another tenant ─────────────────────


def test_drill_tenant_admin_confined_to_its_tenant(env):
    acme_admin = _principal("acme", "adm")
    # Same tenant: allowed.
    admin.provision_user("acme", "newbie", "member", PW, actor=acme_admin)
    # Cross-tenant: refused (the PDP tenant-match denies).
    with pytest.raises(admin.UserManagementError):
        admin.provision_user("beta", "intruder", "member", PW, actor=acme_admin)
    with pytest.raises(admin.UserManagementError):
        admin.deactivate_user("beta", "mem", actor=acme_admin)
    # A member can't manage users at all.
    with pytest.raises(admin.UserManagementError):
        admin.provision_user("acme", "x", "member", PW, actor=_principal("acme", "mem"))


# ── DRILL 6: deactivation force-revokes a user's credentials + tokens ──────


def test_drill_deactivation_force_revokes(env):
    svc = tokens.TokenService()
    mem = _principal("acme", "mem")
    sess, _ = svc.issue_session(mem)
    pat, _ = svc.issue_pat(mem, "ci", [authz.READ])
    assert svc.validate(sess) and svc.validate(pat)

    res = admin.deactivate_user("acme", "mem", actor=_principal("acme", "adm"))
    assert res["tokens_revoked"] >= 2 and res["credentials_revoked"] >= 1
    # Every token is dead immediately, and the password no longer authenticates.
    for raw in (sess, pat):
        with pytest.raises(identity.Deny):
            svc.validate(raw)
    with pytest.raises(providers.AuthenticationFailed):
        providers.LocalPasswordProvider().authenticate("acme", "mem", PW)


# ── DRILL 7: the injected token no longer authenticates ────────────────────


def test_drill_injected_token_is_dead(env, monkeypatch):
    monkeypatch.setattr(config, "MNESIS_MCP_TOKEN", "the-old-global-token", raising=False)
    # Neither the web nor the MCP bearer resolver accepts the shared token.
    with pytest.raises(identity.Deny):
        tokens.resolve_bearer("the-old-global-token")
    app = Starlette()
    webapi.mount_api(app)
    webauth.install(app)
    with TestClient(app) as c:
        r = c.get("/api/pages", headers={"Authorization": "Bearer the-old-global-token"})
        assert r.status_code == 401


# ── DRILL 8: auth events are audited without secrets ───────────────────────


def test_drill_auth_events_audited_without_secrets(env):
    log = providers.AuthAuditLog()
    audit.enable_pdp_audit()
    try:
        # a login (audited by the provider), a token issue, and a PDP denial
        mem = _principal("acme", "mem")                     # auth_success
        svc = tokens.TokenService()
        pat, _ = svc.issue_pat(mem, "ci", [authz.READ])
        audit.record("token_issued", tenant_id="acme", principal_id="mem",
                     action="pat:create", result="ok")
        with _bound(pat):
            with pytest.raises(authz.AuthorizationError):
                mcp_server.mnesis_ingest("x", "s")          # pdp_deny (audited via sink)
    finally:
        audit.disable_pdp_audit()

    events = [e["event"] for e in log.all()]
    assert "auth_success" in events and "token_issued" in events and "pdp_deny" in events
    # No secret (password, token, or hash) is ever recorded.
    blob = json.dumps(log.all())
    assert PW not in blob and pat not in blob and identity.hash_token(pat) not in blob


# ── DRILL 9: bootstrap creates the first admin with no default password ────


def test_drill_bootstrap_no_default_password(env):
    creds = identity.IdentityStore()
    # System-admin bootstrap requires an operator password and is guarded.
    with pytest.raises(providers.PasswordPolicyError):
        admin.bootstrap_system_admin("root", "short")       # weak → refused, nothing written
    admin.bootstrap_system_admin("root", "a-strong-operator-password")
    assert creds.has_system_admin()
    with pytest.raises(admin.AlreadyBootstrapped):
        admin.bootstrap_system_admin("root", "another-strong-password")  # never clobbers
    # There is no hardcoded/default credential: a wrong guess never authenticates.
    with pytest.raises(identity.AuthError):
        auth.resolve_admin("password")


# ── helpers ─────────────────────────────────────────────────────────────────

import contextlib  # noqa: E402


@contextlib.contextmanager
def _bound(raw_key: str):
    ctx, principal = tokens.resolve_bearer(raw_key)
    with tenancy.use(ctx):
        tok = auth.bind_principal(principal)
        try:
            yield
        finally:
            auth.unbind_principal(tok)
