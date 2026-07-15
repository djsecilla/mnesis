"""R2 — configuration bootstrap of the initial admin.

The system starts with exactly ONE usable admin, provisioned from configuration (never a
default password), in the ``must_change_password`` state. Bootstrap is idempotent and
non-destructive: a re-run never resets, re-enables, or changes an existing admin. An
absent/weak credential fails clearly rather than defaulting. Credentials are hashed at
rest and never logged; every bootstrap (and no-op) is audited.
"""

from __future__ import annotations

import json

import pytest

from mnesis import admin, config, identity, providers, store, tenancy
from mnesis.identity import IdentityStore

PW = "correct horse battery staple"
WEAK = "short"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    root = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_ROOT", root, raising=False)
    monkeypatch.setattr(config, "MNESIS_LLM_STUB", True, raising=False)
    monkeypatch.setattr(config, "MNESIS_ADMIN_USERNAME", "admin", raising=False)
    monkeypatch.setattr(config, "MNESIS_ADMIN_TENANT", config.DEFAULT_TENANT_ID, raising=False)
    return root


# ── empty system → one admin + tenant + default vault, must_change_password ──


def test_bootstrap_creates_one_admin_tenant_vault(env):
    res = admin.bootstrap_initial_admin(username="admin", password=PW, data_root=env)
    assert res["created"] is True
    assert res["role"] == "admin" and res["must_change_password"] is True
    assert res["tenant_id"] == config.DEFAULT_TENANT_ID and res["vault_id"] == config.DEFAULT_VAULT_ID

    # The tenant + its default vault physically exist and are usable.
    vctx = tenancy.context_for(res["tenant_id"], res["vault_id"], data_root=env)
    assert vctx.root_path.exists() and vctx.pages_dir.exists()

    # Exactly one admin principal, role=admin, in the must_change_password state.
    store_ = IdentityStore(env / config.CREDENTIALS_FILENAME)
    admins = [u for u in store_.principals_for_tenant(config.DEFAULT_TENANT_ID)
              if any(identity.canonical_role(r) == "admin" for r in u["roles"]) and u["active"]]
    assert [u["principal_id"] for u in admins] == ["admin"]
    rec = store_.get(res["credential_id"])
    assert rec.must_change_password is True and rec.public_dict()["must_change_password"] is True


def test_bootstrap_uses_configuration_when_args_omitted(env, monkeypatch):
    monkeypatch.setattr(config, "MNESIS_ADMIN_USERNAME", "root", raising=False)
    monkeypatch.setattr(config, "MNESIS_ADMIN_PASSWORD", PW, raising=False)
    res = admin.bootstrap_initial_admin(data_root=env)  # everything from config
    assert res["created"] and res["principal_id"] == "root"
    # The configured password authenticates the configured admin.
    p = providers.LocalPasswordProvider(store=IdentityStore(env / config.CREDENTIALS_FILENAME))
    assert p.authenticate(config.DEFAULT_TENANT_ID, "root", PW).role == "admin"


# ── idempotent + non-destructive: re-run is a NO-OP, never resets ───────────


def test_rerun_is_a_noop_and_does_not_reset(env):
    first = admin.bootstrap_initial_admin(username="admin", password=PW, data_root=env)
    store_ = IdentityStore(env / config.CREDENTIALS_FILENAME)
    hash_before = store_.get(first["credential_id"]).secret_hash

    # Re-run with a DIFFERENT password + role intent — must change nothing.
    second = admin.bootstrap_initial_admin(username="admin", password="a-totally-different-pw-9", data_root=env)
    assert second["created"] is False and second["reason"] == "admin_exists"
    assert second["existing_admins"] == ["admin"]

    # The existing admin's password hash and role are UNTOUCHED.
    rec = store_.get(first["credential_id"])
    assert rec.secret_hash == hash_before                       # password not reset
    assert identity.canonical_role(rec.role) == "admin"          # role not changed
    # The original password still authenticates; the different one does not.
    p = providers.LocalPasswordProvider(store=store_)
    assert p.authenticate(config.DEFAULT_TENANT_ID, "admin", PW).principal_id == "admin"
    with pytest.raises(providers.AuthenticationFailed):
        p.authenticate(config.DEFAULT_TENANT_ID, "admin", "a-totally-different-pw-9")


def test_noop_when_an_admin_exists_even_under_a_different_username(env):
    admin.bootstrap_initial_admin(username="alice", password=PW, data_root=env)
    # A second bootstrap naming a *different* admin is still a no-op (an admin exists).
    res = admin.bootstrap_initial_admin(username="mallory", password=PW, data_root=env)
    assert res["created"] is False and res["reason"] == "admin_exists"
    store_ = IdentityStore(env / config.CREDENTIALS_FILENAME)
    assert "mallory" not in {u["principal_id"] for u in store_.principals_for_tenant(config.DEFAULT_TENANT_ID)}


# ── absent / weak credential fails clearly, never defaults ──────────────────


def test_absent_credential_fails_clearly(env):
    for bad in (None, "", "   "):
        with pytest.raises(admin.BootstrapError) as ei:
            admin.bootstrap_initial_admin(username="admin", password=bad, data_root=env)
        assert ei.value.reason == "no_credential" and "no default" in str(ei.value).lower()
    # And nothing was provisioned (fail closed — no admin, no half-built state).
    store_ = IdentityStore(env / config.CREDENTIALS_FILENAME)
    assert store_.principals_for_tenant(config.DEFAULT_TENANT_ID) == []


def test_weak_credential_is_refused(env):
    with pytest.raises(providers.PasswordPolicyError):
        admin.bootstrap_initial_admin(username="admin", password=WEAK, data_root=env)
    store_ = IdentityStore(env / config.CREDENTIALS_FILENAME)
    assert store_.principals_for_tenant(config.DEFAULT_TENANT_ID) == []  # nothing created


# ── the credential is stored hashed and never logged ────────────────────────


def test_credential_stored_hashed_and_not_logged(env):
    res = admin.bootstrap_initial_admin(username="admin", password=PW, data_root=env)

    # On disk: argon2id hash, and the plaintext password appears NOWHERE.
    creds_text = (env / config.CREDENTIALS_FILENAME).read_text(encoding="utf-8")
    assert PW not in creds_text
    doc = json.loads(creds_text)["credentials"][res["credential_id"]]
    assert doc["secret_type"] == "password" and doc["hash_algo"] == "argon2id"
    assert doc["secret_hash"].startswith("$argon2") and PW not in doc["secret_hash"]

    # The system audit recorded the bootstrap WITHOUT the credential.
    audit_text = (env / config.SYSTEM_AUDIT_FILENAME).read_text(encoding="utf-8")
    assert PW not in audit_text
    events = [json.loads(l) for l in audit_text.splitlines() if l.strip()]
    boot = [e for e in events if e["action"] == "bootstrap_initial_admin"]
    assert boot and boot[-1]["principal_id"] == "admin" and boot[-1]["must_change_password"] is True
    assert "password" not in boot[-1] and "secret" not in json.dumps(boot[-1])


def test_noop_is_audited(env):
    admin.bootstrap_initial_admin(username="admin", password=PW, data_root=env)
    admin.bootstrap_initial_admin(username="admin", password=PW, data_root=env)  # no-op
    events = [json.loads(l) for l in (env / config.SYSTEM_AUDIT_FILENAME)
              .read_text(encoding="utf-8").splitlines() if l.strip()]
    assert any(e["action"] == "bootstrap_initial_admin_noop" for e in events)


# ── the legacy shim still works (delegates to the R2 path) ──────────────────


def test_legacy_tenant_admin_shim_delegates(env):
    res = admin.bootstrap_tenant_admin(config.DEFAULT_TENANT_ID, "admin", PW, data_root=env)
    assert res["created"] and res["must_change_password"] is True
