"""IAM1 — the unified identity core (`mnesis.identity`).

The one principal / credential / role model every surface and the PDP resolve
through. A valid credential resolves to the right :class:`AuthenticatedPrincipal`
and tenant; invalid / expired / revoked / unknown all **deny** (fail closed);
secrets are stored **hashed** (plaintext never persisted — tokens via sha256,
passwords via argon2id); and the T3 credential rows migrate forward with access
preserved.
"""

from __future__ import annotations

import json
import time

import pytest

from mnesis import auth, config, identity, store, tenancy
from mnesis.store import Page


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """A data root with two tenants (alpha, beta), each one private page, and a fresh
    identity store. Returns ``(data_root, IdentityStore)``."""
    root = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_ROOT", root, raising=False)
    monkeypatch.setattr(config, "MNESIS_LLM_STUB", True, raising=False)
    a = tenancy.create_tenant("alpha", data_root=root)
    b = tenancy.create_tenant("beta", data_root=root)
    with tenancy.use(a):
        store.write_page(Page(id="a-page", title="Alpha only", body="alpha secret"))
    with tenancy.use(b):
        store.write_page(Page(id="b-page", title="Beta only", body="beta secret"))
    return root, identity.IdentityStore()


# ── the model ───────────────────────────────────────────────────────────────


def test_role_permission_and_scope_model():
    # Roles map to permission sets; the PDP (later) consumes this.
    assert identity.BUILTIN_ROLES["admin"].permissions == frozenset(
        {identity.READ, identity.WRITE, identity.MAINTAIN, identity.ADMIN}
    )
    assert identity.BUILTIN_ROLES["readonly"].permissions == frozenset({identity.READ})
    assert identity.permissions_for({"member"}) == frozenset(
        {identity.READ, identity.WRITE, identity.MAINTAIN}
    )
    # AuthenticatedPrincipal carries roles + scopes and derives permissions.
    ap = identity.AuthenticatedPrincipal(
        tenant_id="alpha", principal_id="alice", roles=frozenset({"member"}),
        scopes=frozenset({"mnesis:read"}), kind=identity.HUMAN,
    )
    assert ap.has_role("member") and ap.has_scope("mnesis:read")
    assert ap.has_permission(identity.WRITE) and not ap.has_permission(identity.ADMIN)
    assert ap.role == "member"  # scalar view for legacy consumers


# ── resolve → AuthenticatedPrincipal + tenant ───────────────────────────────


def test_valid_credential_resolves_to_right_principal_and_tenant(env):
    _root, creds = env
    raw, rec = creds.issue("alpha", "alice", "admin", scopes=["mnesis:admin"])
    assert rec.tenant_id == "alpha" and rec.role == "admin"

    ap = identity.resolve(raw)
    assert isinstance(ap, identity.AuthenticatedPrincipal)
    assert ap.tenant_id == "alpha" and ap.principal_id == "alice"
    assert ap.roles == frozenset({"admin"}) and ap.scopes == frozenset({"mnesis:admin"})
    assert ap.kind == identity.HUMAN

    # The surface adapter also opens + scopes the tenant to alpha only.
    ctx, principal = auth.resolve_principal(raw)
    assert ctx.tenant_id == "alpha" and principal.role == "admin"
    with tenancy.use(ctx):
        assert [p.id for p in store.list_pages()] == ["a-page"]
        with pytest.raises(FileNotFoundError):
            store.read_page("b-page")


def test_agent_kind_inferred_and_recorded(env):
    _root, creds = env
    raw, rec = creds.issue("alpha", "ci-bot", "agent", name="ci")
    assert rec.kind == identity.AGENT
    assert identity.resolve(raw).kind == identity.AGENT


def test_tenant_derives_only_from_credential(env):
    _root, creds = env
    raw_a, _ = creds.issue("alpha", "alice", "admin")
    raw_b, _ = creds.issue("beta", "brenda", "member")
    # There is no parameter through which a caller could supply a tenant.
    assert identity.resolve(raw_a).tenant_id == "alpha"
    assert identity.resolve(raw_b).tenant_id == "beta"


# ── deny paths (fail closed) ────────────────────────────────────────────────


def test_absent_or_unknown_credential_denies(env):
    _root, creds = env
    for bad in (None, "", "not-a-real-token", "Bearer x"):
        assert creds.validate(bad) is None
        with pytest.raises(identity.Deny):
            identity.resolve(bad)
        # Deny is an AuthError/InvalidCredential subclass — existing boundaries still catch it.
        with pytest.raises(auth.AuthError):
            auth.resolve_principal(bad)


def test_expired_credential_denies(env):
    _root, creds = env
    raw, _ = creds.issue("alpha", "bob", "member", expires_at=time.time() - 1)
    assert creds.validate(raw) is None
    with pytest.raises(identity.Deny):
        identity.resolve(raw)


def test_revoked_credential_denies(env):
    _root, creds = env
    raw, rec = creds.issue("alpha", "ci-bot", "agent")
    assert identity.resolve(raw).principal_id == "ci-bot"  # works before revoke
    assert creds.revoke(rec.id) is True
    assert creds.revoke(rec.id) is False  # idempotent
    assert creds.get(rec.id).revoked_at is not None  # timestamp recorded
    assert creds.validate(raw) is None
    with pytest.raises(identity.Deny):
        identity.resolve(raw)


def test_system_admin_credential_is_not_a_tenant_principal(env):
    _root, creds = env
    raw, _ = creds.issue_system_admin("root")
    # As a tenant principal → denied; as an admin → resolves.
    with pytest.raises(identity.Deny):
        identity.resolve(raw)
    adminp = auth.resolve_admin(raw)
    assert adminp.tenant_id == identity.SYSTEM_TENANT and adminp.role == identity.SYSTEM_ROLE
    assert auth.is_system_admin(adminp)
    # A tenant credential is refused by the admin resolver (fail closed, both ways).
    raw_t, _ = creds.issue("alpha", "alice", "admin")
    with pytest.raises(identity.Deny):
        auth.resolve_admin(raw_t)


def test_suspended_tenant_denies(env):
    root, creds = env
    raw, _ = creds.issue("alpha", "alice", "admin")
    tenancy.TenantRegistry(root / config.REGISTRY_FILENAME).set_status("alpha", "suspended")
    with pytest.raises(identity.Deny):
        identity.resolve(raw)


def test_invalid_role_is_refused(env):
    _root, creds = env
    with pytest.raises(identity.InvalidRole):
        creds.issue("alpha", "x", "superuser")


# ── secrets at rest (hashed, plaintext never persisted) ─────────────────────


def test_token_secret_is_hashed_at_rest(env):
    root, creds = env
    raw, _ = creds.issue("alpha", "alice", "admin")
    on_disk = (root / "credentials.json").read_text(encoding="utf-8")
    assert raw not in on_disk                    # the token itself is never persisted
    assert identity.hash_token(raw) in on_disk   # only its sha256 hash is stored
    # Neither the listing view nor the record's public view leaks a hash.
    pub = [c.public_dict() for c in creds.list_for_tenant("alpha")]
    blob = json.dumps(pub)
    assert "token_hash" not in blob and "secret_hash" not in blob
    assert identity.hash_token(raw) not in blob


def test_password_secret_is_argon2id_at_rest_and_verifies(env):
    root, creds = env
    rec = creds.issue_password("alpha", "carol", "member", "hunter2-correct-horse")
    on_disk = (root / "credentials.json").read_text(encoding="utf-8")
    assert "hunter2-correct-horse" not in on_disk        # plaintext never persisted
    assert rec.hash_algo == identity.ALGO_ARGON2ID
    assert on_disk.count("$argon2id$") >= 1              # an argon2id hash is stored
    # Login verifies the right password and rejects a wrong one.
    assert creds.verify_login("alpha", "carol", "hunter2-correct-horse") is not None
    assert creds.verify_login("alpha", "carol", "wrong") is None
    # A password credential is NOT bearer-resolvable via the token path.
    assert creds.validate("hunter2-correct-horse") is None


# ── T3 migration: legacy rows preserve access ───────────────────────────────


def test_legacy_t3_rows_migrate_and_preserve_access(env):
    """A credentials.json written in the T3 schema (token_hash / role / revoked) still
    authenticates through the identity core, with roles/kind derived."""
    root, _creds = env
    raw_active = "legacy-active-token"
    raw_revoked = "legacy-revoked-token"
    legacy = {
        "credentials": {
            "c1": {
                "id": "c1",
                "token_hash": identity.hash_token(raw_active),
                "tenant_id": "alpha",
                "principal_id": "olduser",
                "role": "member",
                "created": "2026-01-01T00:00:00.000000Z",
                "expires_at": None,
                "revoked": False,
                "name": "legacy",
            },
            "c2": {
                "id": "c2",
                "token_hash": identity.hash_token(raw_revoked),
                "tenant_id": "alpha",
                "principal_id": "goneuser",
                "role": "admin",
                "created": "2026-01-01T00:00:00.000000Z",
                "expires_at": None,
                "revoked": True,
                "name": "legacy-revoked",
            },
        }
    }
    (root / "credentials.json").write_text(json.dumps(legacy), encoding="utf-8")

    creds = identity.IdentityStore()
    # The active legacy row still authenticates; role migrates to the roles set.
    ap = identity.resolve(raw_active)
    assert ap.tenant_id == "alpha" and ap.principal_id == "olduser"
    assert ap.roles == frozenset({"member"}) and ap.role == "member"
    # The revoked legacy row still denies (revoked bool → revoked_at).
    assert creds.get("c2").revoked_at is not None
    with pytest.raises(identity.Deny):
        identity.resolve(raw_revoked)

    # An explicit migrate() rewrites the file into the rich schema without losing access.
    upgraded = creds.migrate()
    assert upgraded == 2
    rewritten = json.loads((root / "credentials.json").read_text(encoding="utf-8"))
    c1 = rewritten["credentials"]["c1"]
    assert c1["secret_hash"] == identity.hash_token(raw_active)
    assert c1["roles"] == ["member"] and c1["secret_type"] == "token"
    assert "token_hash" not in c1  # normalized away
    assert identity.resolve(raw_active).principal_id == "olduser"  # access preserved


def test_auth_facade_reexports_the_core(env):
    """auth.py is a facade — the same objects, so existing imports keep working."""
    assert auth.CredentialStore is identity.IdentityStore
    assert auth.Credential is identity.CredentialRecord
    assert auth.Principal is identity.Principal
    assert auth.resolve is identity.resolve
    _root, creds = env
    raw, _ = creds.issue("alpha", "alice", "admin")
    # A Principal from the facade is a core Principal with the scalar-role BC surface.
    _ctx, p = auth.resolve_principal(raw)
    assert isinstance(p, identity.Principal) and p.role == "admin"
    assert auth.Principal("p1", "acme", "member").role == "member"  # T3 positional ctor
