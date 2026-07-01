"""IAM3 — session & token services (issue / validate / refresh / revoke).

The runtime credential mechanics for the three surfaces: web sessions (idle + absolute
expiry, rotation, immediate logout), Personal Access Tokens (named, scoped subset,
expiring, revocable), and agent/machine API keys (least-privilege scoped, rotatable).
All opaque + hashed at rest, constant-time compared, resolving to the IAM1
:class:`AuthenticatedPrincipal` (with scopes) or :class:`Deny`.
"""

from __future__ import annotations

import time

import pytest

from mnesis import config, identity, tenancy, tokens
from mnesis.identity import ADMIN, MAINTAIN, READ, WRITE


@pytest.fixture()
def svc(tmp_path, monkeypatch):
    """A token service over a temp data root, with a provisioned tenant 'acme'."""
    root = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_ROOT", root, raising=False)
    monkeypatch.setattr(config, "MNESIS_LLM_STUB", True, raising=False)
    tenancy.create_tenant("acme", data_root=root)
    return tokens.TokenService(path=root / "tokens.json")


def _member(scopes=()):
    return identity.Principal("alice", "acme", "member", roles=frozenset({"member"}), scopes=frozenset(scopes))


# ── web sessions: validate, logout, expiry, rotation ────────────────────────


def test_session_validates_then_fails_after_logout(svc):
    raw, rec = svc.issue_session(_member())
    ap = svc.validate(raw)
    assert isinstance(ap, identity.AuthenticatedPrincipal)
    assert ap.principal_id == "alice" and ap.tenant_id == "acme" and "member" in ap.roles
    # Logout (immediate, server-side) → the very next validation denies.
    assert svc.logout(raw) is True
    with pytest.raises(identity.Deny):
        svc.validate(raw)


def test_session_absolute_expiry(svc):
    raw, _ = svc.issue_session(_member(), absolute_lifetime=1, idle_timeout=3600)
    assert svc.validate(raw).principal_id == "alice"
    # Past the absolute cap it denies (checked via an explicit clock).
    with pytest.raises(identity.Deny):
        svc.validate(raw, now=time.time() + 5)


def test_session_idle_expiry_is_sliding(svc):
    t0 = time.time()
    raw, _ = svc.issue_session(_member(), idle_timeout=100, absolute_lifetime=100000, now=t0)
    # A use inside the idle window slides it forward…
    assert svc.validate(raw, now=t0 + 50).principal_id == "alice"
    assert svc.validate(raw, now=t0 + 120).principal_id == "alice"  # 70s after last use, still ok
    # …but a long silence past the idle window denies.
    with pytest.raises(identity.Deny):
        svc.validate(raw, now=t0 + 300)


def test_refresh_rotates_and_invalidates_the_old_session(svc):
    old_raw, old_rec = svc.issue_session(_member(), absolute_lifetime=10_000)
    new_raw, new_rec = svc.refresh_session(old_raw)
    assert new_raw != old_raw and new_rec.id != old_rec.id
    # New token works; old token is immediately dead (rotation = invalidate old).
    assert svc.validate(new_raw).principal_id == "alice"
    with pytest.raises(identity.Deny):
        svc.validate(old_raw)
    # Rotation preserves the absolute deadline (hard cap not extended) + lineage.
    assert new_rec.absolute_expires_at == old_rec.absolute_expires_at
    assert new_rec.rotated_from == old_rec.id
    assert svc.get(old_rec.id).rotated_to == new_rec.id


# ── PATs: scope subset, expiry, revoke ──────────────────────────────────────


def test_pat_validates_within_scope_and_fails_after_revoke(svc):
    principal = _member()  # member => {read, write, maintain}
    raw, rec = svc.issue_pat(principal, "ci-deploy", [READ, WRITE], ttl=10_000)
    ap = svc.validate(raw)
    assert ap.scopes == frozenset({READ, WRITE})  # scopes travel with the credential
    assert rec.name == "ci-deploy" and rec.token_type == tokens.PAT
    # Revoke → immediate deny.
    assert svc.revoke(rec.id) is True
    with pytest.raises(identity.Deny):
        svc.validate(raw)


def test_pat_scope_must_be_subset_of_issuer_permissions(svc):
    principal = _member()  # member lacks ADMIN
    with pytest.raises(tokens.ScopeError):
        svc.issue_pat(principal, "too-powerful", [READ, ADMIN])


def test_pat_expiry_denies(svc):
    raw, _ = svc.issue_pat(_member(), "shortlived", [READ], ttl=1)
    assert svc.validate(raw).principal_id == "alice"
    with pytest.raises(identity.Deny):
        svc.validate(raw, now=time.time() + 5)


# ── agent keys: scopes carried, least privilege, rotation ───────────────────


def test_agent_key_carries_its_scopes(svc):
    raw, rec = svc.issue_agent_key("acme", "dream-agent", ["agent"], [READ, MAINTAIN], name="dreamer")
    ap = svc.validate(raw)
    assert ap.kind == identity.AGENT
    assert ap.scopes == frozenset({READ, MAINTAIN}) and "agent" in ap.roles
    assert rec.token_type == tokens.AGENT_KEY


def test_agent_key_least_privilege_rejects_excess_scope(svc):
    # The 'agent' role never grants ADMIN, so an ADMIN-scoped agent key is refused.
    with pytest.raises(tokens.ScopeError):
        svc.issue_agent_key("acme", "rogue", ["agent"], [ADMIN])


def test_agent_key_rotation_replaces_and_revokes_old(svc):
    old_raw, old_rec = svc.issue_agent_key("acme", "svc", ["agent"], [READ], ttl=10_000)
    new_raw, new_rec = svc.rotate(old_rec.id)
    assert svc.validate(new_raw).principal_id == "svc"
    assert new_rec.scopes == old_rec.scopes  # scopes preserved across rotation
    with pytest.raises(identity.Deny):
        svc.validate(old_raw)  # old key dead immediately


# ── cross-cutting: hashed at rest, constant-time, fail-closed ───────────────


def test_all_tokens_stored_hashed_never_plaintext(svc):
    s_raw, _ = svc.issue_session(_member())
    p_raw, _ = svc.issue_pat(_member(), "pat", [READ])
    a_raw, _ = svc.issue_agent_key("acme", "agent", ["agent"], [READ])
    on_disk = svc.path.read_text(encoding="utf-8")
    for raw in (s_raw, p_raw, a_raw):
        assert raw not in on_disk
        assert identity.hash_token(raw) in on_disk


def test_public_view_omits_the_hash(svc):
    _raw, rec = svc.issue_pat(_member(), "pat", [READ])
    pub = svc.get(rec.id).public_dict()
    assert "token_hash" not in pub and pub["revoked"] is False


def test_absent_and_unknown_tokens_deny(svc):
    for bad in (None, "", "mns_pat_nope"):
        with pytest.raises(identity.Deny):
            svc.validate(bad)


def test_revoked_credential_denies_even_before_expiry(svc):
    raw, rec = svc.issue_pat(_member(), "pat", [READ], ttl=10_000)
    svc.revoke(rec.id)
    # A dedicated revocation store makes revoke immediate and independent of expiry.
    assert svc.revocations.contains(rec.id)
    with pytest.raises(identity.Deny):
        svc.validate(raw, now=time.time() + 1)


def test_suspended_tenant_denies_all_its_tokens(svc):
    raw, _ = svc.issue_pat(_member(), "pat", [READ], ttl=10_000)
    assert svc.validate(raw).principal_id == "alice"
    tenancy.TenantRegistry().set_status("acme", "suspended")
    with pytest.raises(identity.Deny):
        svc.validate(raw)


def test_revoke_all_for_principal(svc):
    r1, _ = svc.issue_pat(_member(), "one", [READ])
    r2, rec2 = svc.issue_agent_key("acme", "alice", ["agent"], [READ])
    n = svc.revoke_all_for_principal("acme", "alice")
    assert n == 2
    for raw in (r1, r2):
        with pytest.raises(identity.Deny):
            svc.validate(raw)
