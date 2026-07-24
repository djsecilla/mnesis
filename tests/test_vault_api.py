"""V7 — the vault management API endpoints (`/api/vaults`).

Each endpoint is a thin caller of the V6 vault lifecycle service + `authz.resolve_vault`
(the re-authorization point) + the PDP. These tests prove: a principal lists only its OWN
vaults; create provisions a vault with the default config within its tenant and respects the
tenant quota; rename changes the display name but NEVER the vault_id or on-disk path; delete
removes the data (guarded) and refuses the last remaining vault; activate re-authorizes and
switches only to a granted vault; targeting another principal's/tenant's vault on ANY
endpoint is denied without leaking existence; an admin gets NO extra vault visibility; and
every mutation is audited (never knowledge content).
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from mnesis import account, admin, config, webapi, webauth

PW = "correct horse battery staple"


@pytest.fixture()
def app(monkeypatch):
    tmp = Path(tempfile.mkdtemp(prefix="mnesis-vaultapi-"))
    monkeypatch.setattr(config, "DATA_ROOT", tmp / "data", raising=False)
    monkeypatch.setattr(config, "MNESIS_LLM_STUB", True, raising=False)
    monkeypatch.setattr(config, "MNESIS_WEB_COOKIE_SECURE", False, raising=False)
    monkeypatch.setattr(config, "MNESIS_AUTH_ENABLED", True, raising=False)
    admin.bootstrap_initial_admin(username="admin", password=PW, data_root=config.DATA_ROOT)
    application = Starlette()
    webapi.mount_api(application)
    webauth.install(application)
    yield application
    shutil.rmtree(tmp, ignore_errors=True)


def _login(c: TestClient, user: str, pw: str, *, tenant: str | None = None) -> None:
    r = c.post("/api/auth/login", json={"tenant_id": tenant or user, "username": user, "password": pw})
    assert r.status_code == 200, r.text


def _csrf(c: TestClient) -> dict:
    return {"X-CSRF-Token": c.cookies["mnesis_csrf"]}


def _admin(app) -> tuple[TestClient, dict]:
    c = TestClient(app)
    _login(c, "admin", PW, tenant=config.DEFAULT_TENANT_ID)
    c.post("/api/auth/change-password",
           json={"current_password": PW, "new_password": "admin-real-passphrase-1"}, headers=_csrf(c))
    return c, _csrf(c)


def _user_client(app, admin_c: TestClient, csrf: dict, name: str) -> tuple[TestClient, dict]:
    """Provision `name` via the admin, clear its forced-change, and return a logged-in
    (full-session) client for that user managing its OWN tenant/vaults."""
    created = admin_c.post("/api/admin/users", json={"username": name, "role": "user"}, headers=csrf).json()
    real = f"{name}-real-passphrase-1"
    account.change_own_password(name, name, created["initial_password"], real)
    c = TestClient(app)
    _login(c, name, real)
    return c, _csrf(c)


def _vault_root(tenant: str, vault_id: str) -> Path:
    return config.DATA_ROOT / config.TENANTS_DIRNAME / tenant / config.VAULTS_DIRNAME / vault_id


def _vault_audit(app) -> list[dict]:
    path = config.vault_audit_path()
    if not path.is_file():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


# ── list: only your own vaults, with the rich shape ─────────────────────────


def test_lists_only_own_vaults(app):
    ac, csrf = _admin(app)
    alice, ac_csrf = _user_client(app, ac, csrf, "alice")
    bob, _ = _user_client(app, ac, csrf, "bob")

    alice.post("/api/vaults", json={"name": "Research"}, headers=ac_csrf)

    body = alice.get("/api/vaults").json()
    by_id = {v["vault_id"]: v for v in body["vaults"]}
    assert {"default", "research"} <= set(by_id)
    r = by_id["research"]
    assert r["name"] == "Research" and r["created"] and r["page_count"] == 0
    assert body["active_vault"] == "default" and by_id["default"]["is_active"] is True

    # Bob (another tenant) never sees alice's vault.
    assert "research" not in {v["vault_id"] for v in bob.get("/api/vaults").json()["vaults"]}


# ── create: provisions within the tenant, default config, quota-gated ───────


def test_create_provisions_within_tenant_with_default_config(app):
    ac, csrf = _admin(app)
    alice, acsrf = _user_client(app, ac, csrf, "alice")

    r = alice.post("/api/vaults", json={"name": "Research"}, headers=acsrf)
    assert r.status_code == 201, r.text
    assert r.json()["vault_id"] == "research" and r.json()["tenant_id"] == "alice"

    # Provisioned within alice's tenant, with a store + a default config (V3).
    assert _vault_root("alice", "research").is_dir()
    cfg = alice.get("/api/vaults/research/config").json()
    assert "person" in cfg["entity_types"] and "uses" in cfg["predicates"]   # the default schema


def test_create_respects_tenant_vault_quota(app, monkeypatch):
    ac, csrf = _admin(app)
    alice, acsrf = _user_client(app, ac, csrf, "alice")
    monkeypatch.setattr(config, "MNESIS_TENANT_MAX_VAULTS", 1, raising=False)

    assert alice.post("/api/vaults", json={"name": "One"}, headers=acsrf).status_code == 201
    over = alice.post("/api/vaults", json={"name": "Two"}, headers=acsrf)
    assert over.status_code >= 400 and over.json()["reason"] == "vault_quota_exceeded"


def test_create_rejects_empty_name(app):
    ac, csrf = _admin(app)
    alice, acsrf = _user_client(app, ac, csrf, "alice")
    assert alice.post("/api/vaults", json={"name": "   "}, headers=acsrf).status_code == 400


# ── rename: display name only — vault_id and path are immutable ─────────────


def test_rename_is_display_only(app):
    ac, csrf = _admin(app)
    alice, acsrf = _user_client(app, ac, csrf, "alice")
    alice.post("/api/vaults", json={"name": "Research"}, headers=acsrf)
    path_before = _vault_root("alice", "research")
    assert path_before.is_dir()

    r = alice.patch("/api/vaults/research", json={"name": "Renamed Research"}, headers=acsrf)
    assert r.status_code == 200 and r.json()["vault_id"] == "research" and r.json()["name"] == "Renamed Research"

    # The vault_id and the on-disk path are unchanged; only the display name moved.
    assert path_before.is_dir()                              # same directory
    assert not _vault_root("alice", "renamed-research").exists()
    by_id = {v["vault_id"]: v for v in alice.get("/api/vaults").json()["vaults"]}
    assert by_id["research"]["name"] == "Renamed Research"

    # An empty rename is refused.
    assert alice.patch("/api/vaults/research", json={"name": ""}, headers=acsrf).status_code == 400


# ── delete: removes data (guarded); refuses the last remaining vault ────────


def test_delete_removes_data_and_refuses_last_vault(app):
    ac, csrf = _admin(app)
    alice, acsrf = _user_client(app, ac, csrf, "alice")
    alice.post("/api/vaults", json={"name": "Research"}, headers=acsrf)
    root = _vault_root("alice", "research")
    assert root.is_dir()

    # Guarded: no / wrong confirm is refused; data untouched.
    assert alice.delete("/api/vaults/research", headers=acsrf).json()["reason"] == "confirm_mismatch"
    assert alice.delete("/api/vaults/research", params={"confirm": "nope"}, headers=acsrf).status_code == 400
    assert root.is_dir()

    # Correct confirm removes the whole vault.
    ok = alice.delete("/api/vaults/research", params={"confirm": "research"}, headers=acsrf)
    assert ok.status_code == 200 and ok.json()["removed_root"] is True
    assert not root.exists()

    # Now only `default` remains → deleting it is refused (no-lockout).
    last = alice.delete("/api/vaults/default", params={"confirm": "default"}, headers=acsrf)
    assert last.status_code == 409 and last.json()["reason"] == "last_vault"


# ── activate: re-authorizes; switches only to a granted vault ───────────────


def test_activate_switches_only_to_granted_vault(app):
    ac, csrf = _admin(app)
    alice, acsrf = _user_client(app, ac, csrf, "alice")
    alice.post("/api/vaults", json={"name": "Research"}, headers=acsrf)

    r = alice.post("/api/vaults/research/activate", json={}, headers=acsrf)
    assert r.status_code == 200 and r.json()["active_vault"] == "research"
    assert alice.post("/api/vaults/default/activate", json={}, headers=acsrf).status_code == 200

    # A vault alice doesn't have → denied, no existence leak (404).
    assert alice.post("/api/vaults/ghost/activate", json={}, headers=acsrf).status_code == 404


# ── isolation: another principal's/tenant's vault is denied, no leak ─────────


def test_cross_tenant_vault_denied_without_leaking_existence(app):
    ac, csrf = _admin(app)
    alice, acsrf = _user_client(app, ac, csrf, "alice")
    bob, bcsrf = _user_client(app, ac, csrf, "bob")
    alice.post("/api/vaults", json={"name": "Research"}, headers=acsrf)  # alice's vault

    # Bob targets alice's "research" on EVERY endpoint → 404 (looks non-existent; no leak).
    assert bob.patch("/api/vaults/research", json={"name": "x"}, headers=bcsrf).status_code == 404
    assert bob.delete("/api/vaults/research", params={"confirm": "research"}, headers=bcsrf).status_code == 404
    assert bob.post("/api/vaults/research/activate", json={}, headers=bcsrf).status_code == 404
    assert bob.get("/api/vaults/research/config").status_code in (403, 404)
    assert "research" not in {v["vault_id"] for v in bob.get("/api/vaults").json()["vaults"]}


def test_admin_has_no_special_vault_access(app):
    ac, csrf = _admin(app)
    carol, ccsrf = _user_client(app, ac, csrf, "carol")
    carol.post("/api/vaults", json={"name": "Research"}, headers=ccsrf)

    # The admin (its own tenant) does not see or reach carol's vault — no special visibility.
    assert "research" not in {v["vault_id"] for v in ac.get("/api/vaults").json()["vaults"]}
    assert ac.post("/api/vaults/research/activate", json={}, headers=csrf).status_code == 404
    assert ac.patch("/api/vaults/research", json={"name": "x"}, headers=csrf).status_code == 404


# ── audit: every mutation recorded (actor + vault + action), no content ─────


def test_mutations_are_audited(app):
    ac, csrf = _admin(app)
    alice, acsrf = _user_client(app, ac, csrf, "alice")
    alice.post("/api/vaults", json={"name": "Research"}, headers=acsrf)
    alice.patch("/api/vaults/research", json={"name": "Renamed"}, headers=acsrf)
    alice.post("/api/vaults/research/activate", json={}, headers=acsrf)
    alice.delete("/api/vaults/research", params={"confirm": "research"}, headers=acsrf)

    events = [e for e in _vault_audit(app) if e.get("actor") == "alice" and e.get("vault_id") == "research"]
    actions = {e["action"] for e in events}
    assert {"create", "rename", "activate", "delete"} <= actions
    assert all(e.get("tenant_id") == "alice" for e in events)
