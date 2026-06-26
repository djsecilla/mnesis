"""T3 — authentication & principal/tenant resolution (CLAUDE.md §16).

Credentials map to ``{tenant_id, principal_id, role}``; a resolver yields an
authenticated ``(TenantContext, Principal)`` at boundaries. The tenant is taken
ONLY from the validated credential — never from a request header/body/path — and
an absent/invalid/expired/revoked credential is DENIED (fail closed, no default
tenant). Secrets are stored hashed, never the raw token.
"""

from __future__ import annotations

import time

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from mnesis import auth, config, mcp_server, store, tenancy
from mnesis.store import Page


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """A data root with two tenants (alpha, beta), each with one private page, and a
    credential store. Returns ``(data_root, cred_store)``."""
    root = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_ROOT", root, raising=False)
    monkeypatch.setattr(config, "MNESIS_LLM_STUB", True, raising=False)
    a = tenancy.create_tenant("alpha", data_root=root)
    b = tenancy.create_tenant("beta", data_root=root)
    with tenancy.use(a):
        store.write_page(Page(id="a-page", title="Alpha only", body="alpha secret"))
    with tenancy.use(b):
        store.write_page(Page(id="b-page", title="Beta only", body="beta secret"))
    return root, auth.CredentialStore()


# ── issue / validate / resolve ──────────────────────────────────────────────


def test_credential_resolves_to_its_tenant_only(env):
    _root, creds = env
    raw, cred = creds.issue("alpha", "alice", "admin")
    assert cred.tenant_id == "alpha" and cred.role == "admin"

    ctx, principal = auth.resolve_principal(raw)
    assert ctx.tenant_id == "alpha"
    assert principal.tenant_id == "alpha" and principal.principal_id == "alice"
    assert principal.role == "admin"
    # Data access under the resolved context is scoped to alpha only.
    with tenancy.use(ctx):
        assert [p.id for p in store.list_pages()] == ["a-page"]
        with pytest.raises(FileNotFoundError):
            store.read_page("b-page")


def test_absent_or_invalid_credential_is_denied(env):
    _root, creds = env
    for bad in (None, "", "not-a-real-token", "Bearer x"):
        assert creds.validate(bad) is None
        with pytest.raises(auth.InvalidCredential):
            auth.resolve_principal(bad)


def test_expired_credential_is_denied(env):
    _root, creds = env
    raw, _cred = creds.issue("alpha", "bob", "member", expires_at=time.time() - 1)
    assert creds.validate(raw) is None
    with pytest.raises(auth.InvalidCredential):
        auth.resolve_principal(raw)


def test_revocation_denies_subsequently(env):
    _root, creds = env
    raw, cred = creds.issue("alpha", "ci-bot", "agent", name="ci")
    assert auth.resolve_principal(raw)[1].principal_id == "ci-bot"  # works before revoke

    assert creds.revoke(cred.id) is True
    assert creds.revoke(cred.id) is False  # idempotent (already revoked)
    assert creds.validate(raw) is None
    with pytest.raises(auth.InvalidCredential):
        auth.resolve_principal(raw)


def test_invalid_role_is_refused(env):
    _root, creds = env
    with pytest.raises(auth.InvalidRole):
        creds.issue("alpha", "x", "superuser")


def test_client_supplied_tenant_id_is_never_trusted_at_the_store(env):
    """The resolver derives the tenant solely from the credential — there is no
    parameter through which a caller could supply one."""
    _root, creds = env
    raw, _ = creds.issue("alpha", "alice", "admin")
    # Even an issued credential for beta resolves to beta, never to a caller's wish.
    raw_b, _ = creds.issue("beta", "brenda", "member")
    assert auth.resolve_principal(raw)[0].tenant_id == "alpha"
    assert auth.resolve_principal(raw_b)[0].tenant_id == "beta"


# ── secrets at rest ─────────────────────────────────────────────────────────


def test_raw_token_is_never_stored_only_its_hash(env):
    root, creds = env
    raw, _cred = creds.issue("alpha", "alice", "admin")
    on_disk = (root / "credentials.json").read_text(encoding="utf-8")
    assert raw not in on_disk                 # the secret itself is never persisted
    assert auth.hash_token(raw) in on_disk    # only the hash is stored
    # The listing view also omits the hash.
    assert "token_hash" not in str([c.public_dict() for c in creds.list_for_tenant("alpha")])


# ── boundary: tenant comes from the credential, client id ignored ───────────


def _app(store_=None):
    async def whoami(request):
        p = auth.current_principal()
        return JSONResponse({
            "tenant": tenancy.current().tenant_id,
            "principal": p.principal_id,
            "role": p.role,
            "pages": [pg.id for pg in store.list_pages()],
        })

    app = Starlette(routes=[Route("/whoami", whoami)])
    app.add_middleware(mcp_server._PrincipalBindingMiddleware, store=store_)
    return app


def test_boundary_resolves_tenant_from_credential_ignoring_supplied_tenant(env):
    _root, creds = env
    raw_a, _ = creds.issue("alpha", "alice", "admin")
    client = TestClient(_app(creds))

    # The request carries a DIFFERENT tenant id (header + query) than the credential's.
    r = client.get(
        "/whoami?tenant_id=beta",
        headers={"Authorization": f"Bearer {raw_a}", "X-Tenant-Id": "beta"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["tenant"] == "alpha"           # the CREDENTIAL's tenant wins
    assert body["principal"] == "alice"
    assert body["pages"] == ["a-page"]         # data access scoped to alpha, not beta


def test_boundary_denies_without_a_valid_credential(env):
    _root, creds = env
    raw_a, cred_a = creds.issue("alpha", "alice", "admin")
    client = TestClient(_app(creds))

    assert client.get("/whoami").status_code == 401                       # no credential
    assert client.get("/whoami", headers={"Authorization": "Bearer nope"}).status_code == 401
    # After revocation the same token is denied (fail closed).
    creds.revoke(cred_a.id)
    assert client.get(
        "/whoami", headers={"Authorization": f"Bearer {raw_a}"}
    ).status_code == 401


def test_two_tenants_credentials_never_cross(env):
    _root, creds = env
    raw_a, _ = creds.issue("alpha", "alice", "admin")
    raw_b, _ = creds.issue("beta", "brenda", "member")
    client = TestClient(_app(creds))

    ra = client.get("/whoami", headers={"Authorization": f"Bearer {raw_a}"}).json()
    rb = client.get("/whoami", headers={"Authorization": f"Bearer {raw_b}"}).json()
    assert ra["tenant"] == "alpha" and ra["pages"] == ["a-page"]
    assert rb["tenant"] == "beta" and rb["pages"] == ["b-page"]
