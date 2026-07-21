"""R7 — admin user-management API endpoints (the Web UI's backend).

Every endpoint under ``/api/admin/users`` is a **thin caller of the R4 service**
(`usermgmt`), authorized server-side at the PDP (admin role) and audited. These tests
prove: an admin can list/create/update(PATCH)/reset/revoke/delete; a non-admin is denied
on EVERY endpoint (even crafted requests); creating a user yields its own tenant + default
vault + a one-time credential shown once (must_change_password); the safety rules
(self-role-change, last-admin demotion/deactivation/deletion) surface as clear errors;
deactivation and delete revoke access immediately; delete removes the user's data; and
every mutation is audited without a secret.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from mnesis import account, admin, config, providers, tenancy, webapi, webauth

PW = "correct horse battery staple"


@pytest.fixture()
def app(monkeypatch):
    """A standalone /api app with a bootstrapped initial admin (must_change_password)."""
    tmp = Path(tempfile.mkdtemp(prefix="mnesis-adminapi-"))
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


def _login(client: TestClient, user: str, pw: str, *, tenant: str | None = None) -> dict:
    r = client.post("/api/auth/login",
                    json={"tenant_id": tenant or user, "username": user, "password": pw})
    assert r.status_code == 200, r.text
    return r.json()


def _csrf(client: TestClient) -> dict:
    return {"X-CSRF-Token": client.cookies["mnesis_csrf"]}


def _ready_admin(app) -> tuple[TestClient, dict]:
    """An admin client past the forced first-login change."""
    c = TestClient(app)
    _login(c, "admin", PW, tenant=config.DEFAULT_TENANT_ID)
    r = c.post("/api/auth/change-password",
               json={"current_password": PW, "new_password": "admin-real-passphrase-1"}, headers=_csrf(c))
    assert r.status_code == 200, r.text
    return c, _csrf(c)


def _make_usable_user(c: TestClient, csrf: dict, username: str) -> str:
    """Admin-create ``username`` and clear its forced-change flag; returns a usable password."""
    created = c.post("/api/admin/users", json={"username": username, "role": "user"}, headers=csrf).json()
    real = f"{username}-real-passphrase-1"
    account.change_own_password(username, username, created["initial_password"], real)
    return real


# ── happy path: an admin can drive the whole lifecycle ──────────────────────


def test_admin_create_yields_tenant_vault_and_one_time_credential(app):
    c, csrf = _ready_admin(app)

    r = c.post("/api/admin/users", json={"username": "carol", "role": "user"}, headers=csrf)
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["username"] == "carol"
    assert created["tenant_id"] == "carol"          # per-user tenancy
    assert created["vault_id"] == config.DEFAULT_VAULT_ID   # its own default vault
    assert created["role"] == "user"
    assert created["must_change_password"] is True
    one_time = created["initial_password"]
    assert one_time

    # The tenant + default vault really exist on disk.
    assert (config.DATA_ROOT / config.TENANTS_DIRNAME / "carol").is_dir()
    vault_ids = [v.vault_id for v in tenancy.list_vaults("carol", data_root=config.DATA_ROOT)]
    assert config.DEFAULT_VAULT_ID in vault_ids

    # The one-time credential works and forces a change; it is never re-listed.
    forced = providers.LocalPasswordProvider().authenticate("carol", "carol", one_time)
    assert forced.must_change_password is True
    users = {u["username"]: u for u in c.get("/api/admin/users").json()["users"]}
    assert "carol" in users and "initial_password" not in users["carol"]


def test_admin_patch_role_and_status_and_reset_and_revoke(app):
    c, csrf = _ready_admin(app)
    c.post("/api/admin/users", json={"username": "carol", "role": "user"}, headers=csrf)

    # PATCH role → admin, then back to user (unified endpoint).
    up = c.patch("/api/admin/users/carol", json={"role": "admin"}, headers=csrf)
    assert up.status_code == 200 and up.json()["role"] == "admin"
    assert {u["username"]: u for u in c.get("/api/admin/users").json()["users"]}["carol"]["role"] == "admin"
    assert c.patch("/api/admin/users/carol", json={"role": "user"}, headers=csrf).status_code == 200

    # reset-password → a new one-time credential (shown once, forces a change).
    reset = c.post("/api/admin/users/carol/reset-password", json={}, headers=csrf)
    assert reset.status_code == 200 and reset.json()["must_change_password"] is True

    # PATCH status inactive/active (deactivate/reactivate) via the unified endpoint.
    deact = c.patch("/api/admin/users/carol", json={"status": "inactive"}, headers=csrf)
    assert deact.status_code == 200 and deact.json()["status"] == "inactive"
    react = c.patch("/api/admin/users/carol", json={"status": "active"}, headers=csrf)
    assert react.status_code == 200 and react.json()["reactivate"]["initial_password"]

    # revoke-credentials (the R7 path name; the legacy /revoke still works too).
    assert c.post("/api/admin/users/carol/revoke-credentials", json={}, headers=csrf).status_code == 200
    assert c.post("/api/admin/users/carol/revoke", json={}, headers=csrf).status_code == 200

    # An empty PATCH is a clear 400 (no-op).
    assert c.patch("/api/admin/users/carol", json={}, headers=csrf).status_code == 400


# ── delete removes the user's data (guarded) ────────────────────────────────


def test_delete_removes_user_tenant_and_data(app):
    c, csrf = _ready_admin(app)
    c.post("/api/admin/users", json={"username": "dave", "role": "user"}, headers=csrf)
    root = config.DATA_ROOT / config.TENANTS_DIRNAME / "dave"
    assert root.is_dir()

    # Guarded: a wrong confirm is refused (400), data untouched.
    bad = c.delete("/api/admin/users/dave", params={"confirm": "WRONG"}, headers=csrf)
    assert bad.status_code == 400 and bad.json()["reason"] == "confirm_mismatch"
    assert root.is_dir()

    # Correct confirm deletes the tenant + vaults + credentials.
    ok = c.delete("/api/admin/users/dave", params={"confirm": "dave"}, headers=csrf)
    assert ok.status_code == 200 and ok.json()["deleted"] is True and ok.json()["removed_root"] is True
    assert not root.exists()
    assert "dave" not in {u["username"] for u in c.get("/api/admin/users").json()["users"]}


# ── a non-admin is denied on EVERY endpoint (server-side, even crafted) ──────


def test_non_admin_denied_on_every_endpoint(app):
    c, csrf = _ready_admin(app)
    real = _make_usable_user(c, csrf, "erin")

    dc = TestClient(app)
    _login(dc, "erin", real)
    d = _csrf(dc)

    assert dc.get("/api/admin/users").status_code == 403
    assert dc.post("/api/admin/users", json={"username": "x", "role": "user"}, headers=d).status_code == 403
    assert dc.patch("/api/admin/users/admin", json={"role": "user"}, headers=d).status_code == 403
    assert dc.patch("/api/admin/users/admin", json={"status": "inactive"}, headers=d).status_code == 403
    assert dc.delete("/api/admin/users/admin", params={"confirm": "admin"}, headers=d).status_code == 403
    assert dc.post("/api/admin/users/admin/role", json={"role": "user"}, headers=d).status_code == 403
    assert dc.post("/api/admin/users/admin/deactivate", json={}, headers=d).status_code == 403
    assert dc.post("/api/admin/users/admin/reactivate", json={}, headers=d).status_code == 403
    assert dc.post("/api/admin/users/admin/reset-password", json={}, headers=d).status_code == 403
    assert dc.post("/api/admin/users/admin/revoke", json={}, headers=d).status_code == 403
    assert dc.post("/api/admin/users/admin/revoke-credentials", json={}, headers=d).status_code == 403


# ── safety rules come from the service as clear, typed errors ───────────────


def test_safety_rules_self_and_last_admin(app):
    c, csrf = _ready_admin(app)  # `admin` is the sole active admin

    # self-role-change is refused (no escalation) — via the unified PATCH.
    self_role = c.patch("/api/admin/users/admin", json={"role": "user"}, headers=csrf)
    assert self_role.status_code == 403 and self_role.json()["reason"] == "self_role_change"

    # the last active admin cannot be deactivated…
    deact = c.patch("/api/admin/users/admin", json={"status": "inactive"}, headers=csrf)
    assert deact.status_code == 403 and deact.json()["reason"] == "last_admin"

    # …nor deleted.
    dele = c.delete("/api/admin/users/admin", params={"confirm": "admin"}, headers=csrf)
    assert dele.status_code == 403 and dele.json()["reason"] == "last_admin"


# ── deactivation and delete revoke access immediately ───────────────────────


def test_deactivation_and_delete_revoke_immediately(app):
    c, csrf = _ready_admin(app)

    # Fred logs in (has a live session), then the admin deactivates him → his session dies.
    real = _make_usable_user(c, csrf, "fred")
    fc = TestClient(app)
    _login(fc, "fred", real)
    assert fc.get("/api/auth/session").status_code == 200          # live before
    assert c.patch("/api/admin/users/fred", json={"status": "inactive"}, headers=csrf).status_code == 200
    assert fc.get("/api/auth/session").status_code == 401          # revoked immediately

    # Gus logs in, then the admin DELETEs him → his session dies too.
    real2 = _make_usable_user(c, csrf, "gus")
    gc = TestClient(app)
    _login(gc, "gus", real2)
    assert gc.get("/api/auth/session").status_code == 200
    assert c.delete("/api/admin/users/gus", params={"confirm": "gus"}, headers=csrf).status_code == 200
    assert gc.get("/api/auth/session").status_code == 401


# ── every mutation is audited, and the audit contains no secret ─────────────


def test_mutations_audited_without_secrets(app):
    c, csrf = _ready_admin(app)

    secrets_seen: list[str] = []
    secrets_seen.append(c.post("/api/admin/users", json={"username": "helen", "role": "user"},
                               headers=csrf).json()["initial_password"])
    secrets_seen.append(c.post("/api/admin/users/helen/reset-password", json={}, headers=csrf)
                        .json()["initial_password"])
    c.patch("/api/admin/users/helen", json={"role": "admin"}, headers=csrf)
    c.patch("/api/admin/users/helen", json={"role": "user"}, headers=csrf)
    c.delete("/api/admin/users/helen", params={"confirm": "helen"}, headers=csrf)

    audit_text = (config.DATA_ROOT / config.AUTH_AUDIT_FILENAME).read_text(encoding="utf-8")
    # The lifecycle events were recorded…
    for event in ("user_created", "user_password_reset", "user_role_assigned", "user_deleted"):
        assert f'"{event}"' in audit_text or f"'{event}'" in audit_text, f"missing audit event {event}"
    # …and no one-time credential ever reached the audit log.
    for secret in secrets_seen:
        assert secret and secret not in audit_text
