"""IAM2 — local identity provider, provider seam, and secure bootstrap.

The default username/password backend (:class:`LocalPasswordProvider`) behind the
pluggable :class:`IdentityProvider` seam: argon2id hashing (plaintext never persisted),
a password policy, brute-force throttling/lockout with auditing, a single-use expiring
reset token, an OIDC seam stub that satisfies the interface, and a secure first-run
system-admin bootstrap that requires operator input and refuses to clobber an existing
admin.
"""

from __future__ import annotations

import time

import pytest

from mnesis import admin, config, identity, providers


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """A data root with one tenant (acme) and a registered password user (alice)."""
    root = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_ROOT", root, raising=False)
    monkeypatch.setattr(config, "MNESIS_LLM_STUB", True, raising=False)
    # Tighten the throttle so lockout is reachable quickly and deterministically.
    monkeypatch.setattr(config, "MNESIS_AUTH_MAX_FAILURES", 3, raising=False)
    monkeypatch.setattr(config, "MNESIS_AUTH_LOCKOUT_SECONDS", 60, raising=False)
    from mnesis import tenancy

    tenancy.create_tenant("acme", data_root=root)
    prov = providers.LocalPasswordProvider()
    prov.register("acme", "alice", "member", "correct horse battery staple")
    return root, prov


# ── authentication: right vs wrong ─────────────────────────────────────────


def test_correct_password_authenticates_to_a_principal(env):
    _root, prov = env
    p = prov.authenticate("acme", "alice", "correct horse battery staple")
    assert isinstance(p, identity.Principal)
    assert p.tenant_id == "acme" and p.principal_id == "alice" and p.role == "member"


def test_wrong_password_fails(env):
    _root, prov = env
    with pytest.raises(providers.AuthenticationFailed):
        prov.authenticate("acme", "alice", "wrong password entirely")
    # An unknown principal fails the same generic way (no user enumeration).
    with pytest.raises(providers.AuthenticationFailed):
        prov.authenticate("acme", "nobody", "correct horse battery staple")


# ── hashing is argon2id and never yields plaintext ─────────────────────────


def test_password_hashed_argon2id_never_plaintext(env):
    root, prov = env
    rec = prov.store.find_password_credential("acme", "alice")
    assert rec.hash_algo == identity.ALGO_ARGON2ID
    assert rec.secret_hash.startswith("$argon2id$")
    on_disk = (root / "credentials.json").read_text(encoding="utf-8")
    assert "correct horse battery staple" not in on_disk
    assert "$argon2id$" in on_disk


# ── password policy ────────────────────────────────────────────────────────


def test_password_policy_rejects_weak_and_short(env):
    _root, prov = env
    for bad in ("", "   ", "short", "password", "changeme"):
        with pytest.raises(providers.PasswordPolicyError):
            prov.register("acme", "weakling", "member", bad)


# ── brute-force throttling / lockout + audit ───────────────────────────────


def test_repeated_failures_lock_the_account(env):
    _root, prov = env
    # MAX_FAILURES == 3 (fixture): three wrong tries, then locked.
    for _ in range(3):
        with pytest.raises(providers.AuthenticationFailed):
            prov.authenticate("acme", "alice", "nope", client_ip="10.0.0.9")
    with pytest.raises(providers.AccountLocked) as ei:
        prov.authenticate("acme", "alice", "correct horse battery staple", client_ip="10.0.0.9")
    assert ei.value.retry_after > 0  # tells the caller when to retry
    # The failures + lockout were audited — and never the password.
    events = [e["event"] for e in prov.audit.all()]
    assert events.count("auth_failure") >= 3 and "auth_locked" in events
    blob = "".join(str(e) for e in prov.audit.all())
    assert "correct horse battery staple" not in blob and "nope" not in blob


def test_per_ip_throttle_spans_accounts(env):
    _root, prov = env
    prov.register("acme", "bob", "member", "another good long secret!!")
    # Spread guesses across two accounts from ONE ip — the ip key still locks.
    for principal in ("alice", "bob", "alice"):
        with pytest.raises(providers.AuthenticationFailed):
            prov.authenticate("acme", principal, "bad guess here", client_ip="1.2.3.4")
    # A fresh, correct login for a third account from the same ip is now blocked.
    prov.register("acme", "carol", "member", "yet another decent secret")
    with pytest.raises(providers.AccountLocked):
        prov.authenticate("acme", "carol", "yet another decent secret", client_ip="1.2.3.4")


def test_success_clears_the_throttle(env):
    _root, prov = env
    for _ in range(2):  # below the limit of 3
        with pytest.raises(providers.AuthenticationFailed):
            prov.authenticate("acme", "alice", "nope", client_ip="9.9.9.9")
    prov.authenticate("acme", "alice", "correct horse battery staple", client_ip="9.9.9.9")
    # Counter reset: two more failures still don't lock (would need 3 fresh ones).
    for _ in range(2):
        with pytest.raises(providers.AuthenticationFailed):
            prov.authenticate("acme", "alice", "nope", client_ip="9.9.9.9")
    assert prov.authenticate(
        "acme", "alice", "correct horse battery staple", client_ip="9.9.9.9"
    ).principal_id == "alice"


# ── reset token: works once, expires ───────────────────────────────────────


def test_reset_token_single_use(env):
    _root, prov = env
    token = prov.begin_reset("acme", "alice")
    assert token
    prov.reset_password("acme", "alice", token, "brand new strong password")
    # Old password no longer works; new one does.
    with pytest.raises(providers.AuthenticationFailed):
        prov.authenticate("acme", "alice", "correct horse battery staple")
    assert prov.authenticate("acme", "alice", "brand new strong password").principal_id == "alice"
    # The token is single-use — replaying it fails.
    with pytest.raises(providers.ResetTokenError):
        prov.reset_password("acme", "alice", token, "some other strong password")


def test_reset_token_expires(env):
    _root, prov = env
    # Issue a token that is already expired.
    raw = prov.resets.issue("acme", "alice", ttl=-1)
    assert prov.resets.consume("acme", "alice", raw) is False
    with pytest.raises(providers.ResetTokenError):
        prov.reset_password("acme", "alice", raw, "a perfectly fine new password")


def test_begin_reset_unknown_account_reveals_nothing(env):
    _root, prov = env
    assert prov.begin_reset("acme", "ghost") is None  # no token, no error (no enumeration)


# ── the provider seam ──────────────────────────────────────────────────────


def test_oidc_stub_satisfies_the_interface_and_fails_closed(env):
    _root, _prov = env
    oidc = providers.OIDCProvider(issuer="https://issuer.example", client_id="mnesis")
    assert isinstance(oidc, providers.IdentityProvider)
    assert oidc.name == "oidc"
    with pytest.raises(providers.AuthenticationError):
        oidc.authenticate("acme", "alice", "any-assertion")


def test_get_identity_provider_selects_by_config(env):
    _root, _prov = env
    assert isinstance(providers.get_identity_provider("local"), providers.LocalPasswordProvider)
    assert isinstance(providers.get_identity_provider("oidc"), providers.OIDCProvider)
    with pytest.raises(identity.AuthError):
        providers.get_identity_provider("saml")  # not registered


# ── secure bootstrap ───────────────────────────────────────────────────────


def test_bootstrap_creates_admin_from_supplied_password(env):
    root, _prov = env
    creds = identity.IdentityStore()
    assert creds.has_system_admin() is False
    cred = admin.bootstrap_system_admin("root", "a-strong-operator-password")
    assert cred.tenant_id == identity.SYSTEM_TENANT and identity.SYSTEM_ROLE in cred.roles
    assert cred.secret_type == identity.SECRET_PASSWORD
    # The operator password is verifiable and argon2id at rest (never plaintext).
    assert creds.verify_login(identity.SYSTEM_TENANT, "root", "a-strong-operator-password")
    on_disk = (root / "credentials.json").read_text(encoding="utf-8")
    assert "a-strong-operator-password" not in on_disk


def test_bootstrap_refuses_to_clobber_existing_admin(env):
    _root, _prov = env
    admin.bootstrap_system_admin("root", "a-strong-operator-password")
    with pytest.raises(admin.AlreadyBootstrapped):
        admin.bootstrap_system_admin("root", "a-different-operator-password")
    # The original admin still authenticates (nothing was reset).
    assert identity.IdentityStore().verify_login(
        identity.SYSTEM_TENANT, "root", "a-strong-operator-password"
    )


def test_bootstrap_has_no_default_and_rejects_weak_password(env):
    _root, _prov = env
    with pytest.raises(providers.PasswordPolicyError):
        admin.bootstrap_system_admin("root", "short")
    # A rejected weak password wrote nothing — no admin was created.
    assert identity.IdentityStore().has_system_admin() is False
