"""R5 — Web UI flows: login/first-login, admin user management, vault self-service.

The server authorizes every request via the PDP — hiding UI is never the control. A
first-login (must_change_password) principal is forced to change its password before any
other screen works; an admin can use the user-management endpoints and a non-admin is
denied server-side (even crafting the request); one-time credentials are shown once; a
user manages its OWN vaults (create/config/switch) and cannot reach another user's.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from mnesis import account, admin, config, providers, tenancy, usermgmt, webapi, webauth

PW = "correct horse battery staple"


@pytest.fixture()
def app(monkeypatch):
    """A standalone /api app with a bootstrapped initial admin (must_change_password)."""
    tmp = Path(tempfile.mkdtemp(prefix="mnesis-webroles-"))
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


def _client(app) -> TestClient:
    return TestClient(app)


def _login(client: TestClient, user: str, pw: str, *, tenant: str | None = None) -> dict:
    # The bootstrapped admin lives in the `default` tenant; created users are per-user
    # tenants (tenant == username). Default the tenant to the username.
    r = client.post("/api/auth/login",
                    json={"tenant_id": tenant or user, "username": user, "password": pw})
    assert r.status_code == 200, r.text
    return {"body": r.json(), "csrf": {"X-CSRF-Token": client.cookies["mnesis_csrf"]}}


def _change_pw(client: TestClient, csrf: dict, cur: str, new: str) -> None:
    r = client.post("/api/auth/change-password",
                    json={"current_password": cur, "new_password": new}, headers=csrf)
    assert r.status_code == 200, r.text


# ── first-login: forced change before any other screen works ────────────────


def test_first_login_forces_password_change(app):
    c = _client(app)
    login = _login(c, "admin", PW, tenant=config.DEFAULT_TENANT_ID)  # bootstrapped admin logs into its own default tenant
    assert login["body"]["must_change_password"] is True
    csrf = login["csrf"]

    # Every other screen is denied server-side while restricted.
    assert c.get("/api/pages").status_code == 403
    assert c.get("/api/admin/users").status_code == 403
    assert c.post("/api/vaults", json={"vault_id": "x"}, headers=csrf).status_code == 403
    assert c.get("/api/auth/session").json()["must_change_password"] is True

    # After changing the password, normal access is restored (session rotated).
    _change_pw(c, csrf, PW, "new-admin-passphrase-1")
    assert c.get("/api/auth/session").json()["must_change_password"] is False
    assert c.get("/api/admin/users").status_code == 200


# ── admin user management: usable by admin, denied to non-admins ────────────


def _ready_admin(app) -> tuple[TestClient, dict]:
    """An admin client past first-login."""
    c = _client(app)
    login = _login(c, "admin", PW, tenant=config.DEFAULT_TENANT_ID)
    _change_pw(c, login["csrf"], PW, "admin-real-passphrase-1")
    # cookies/csrf refreshed on rotation
    return c, {"X-CSRF-Token": c.cookies["mnesis_csrf"]}


def test_admin_creates_manages_users_and_one_time_credential_shown_once(app):
    c, csrf = _ready_admin(app)

    # Create a user (role=user) — the one-time credential is returned ONCE.
    r = c.post("/api/admin/users", json={"username": "carol", "role": "user"}, headers=csrf)
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["role"] == "user" and created["must_change_password"] is True
    one_time = created["initial_password"]
    assert one_time  # shown once here; never returned again

    # It appears in the list (no secret), and the credential really works (forces a change).
    users = {u["username"]: u for u in c.get("/api/admin/users").json()["users"]}
    assert "carol" in users and users["carol"]["role"] == "user"
    assert "initial_password" not in users["carol"]
    forced = providers.LocalPasswordProvider().authenticate("carol", "carol", one_time)
    assert forced.must_change_password is True

    # Change role, reset password (new one-time), deactivate/reactivate — all via the service.
    assert c.post("/api/admin/users/carol/role", json={"role": "admin"}, headers=csrf).status_code == 200
    reset = c.post("/api/admin/users/carol/reset-password", json={}, headers=csrf)
    assert reset.status_code == 200 and reset.json()["initial_password"] != one_time

    # Safety rules come FROM the service as clear errors (not reimplemented in the client):
    # self-role-change is refused…
    self_role = c.post("/api/admin/users/admin/role", json={"role": "user"}, headers=csrf)
    assert self_role.status_code == 403 and self_role.json()["reason"] == "self_role_change"
    # …and demoting the last admin is refused (admin is the only admin now).
    c.post("/api/admin/users/carol/role", json={"role": "user"}, headers=csrf)  # carol back to user
    last = c.post("/api/admin/users/admin/role", json={"role": "user"}, headers=csrf)
    assert last.status_code == 403 and last.json()["reason"] in {"self_role_change", "last_admin"}


def test_non_admin_cannot_see_or_reach_admin_endpoints(app):
    c, csrf = _ready_admin(app)
    # Admin creates a plain user + sets it a usable password.
    created = c.post("/api/admin/users", json={"username": "dan", "role": "user"}, headers=csrf).json()
    account.change_own_password("dan", "dan", created["initial_password"], "dan-real-passphrase-1")

    # Dan logs in (its own tenant) and is a non-admin.
    dc = _client(app)
    _login(dc, "dan", "dan-real-passphrase-1")
    dcsrf = {"X-CSRF-Token": dc.cookies["mnesis_csrf"]}

    # Crafting requests to admin endpoints is DENIED server-side (403), not just hidden.
    assert dc.get("/api/admin/users").status_code == 403
    assert dc.post("/api/admin/users", json={"username": "x", "role": "user"}, headers=dcsrf).status_code == 403
    assert dc.post("/api/admin/users/admin/deactivate", json={}, headers=dcsrf).status_code == 403
    assert dc.post("/api/admin/users/admin/role", json={"role": "user"}, headers=dcsrf).status_code == 403


# ── vault self-service: a user manages its OWN vaults ───────────────────────


def test_user_manages_own_vaults_and_edits_config(app):
    c, csrf = _ready_admin(app)
    created = c.post("/api/admin/users", json={"username": "erin", "role": "user"}, headers=csrf).json()
    account.change_own_password("erin", "erin", created["initial_password"], "erin-real-passphrase-1")

    ec = _client(app)
    _login(ec, "erin", "erin-real-passphrase-1")
    ecsrf = {"X-CSRF-Token": ec.cookies["mnesis_csrf"]}

    # Create a vault it owns.
    r = ec.post("/api/vaults", json={"vault_id": "research", "name": "Research"}, headers=ecsrf)
    assert r.status_code == 201 and r.json()["vault_id"] == "research"

    # It appears in the user's own vault list (owned ∪ granted ∪ default).
    vaults_list = ec.get("/api/vaults").json()["vaults"]
    assert {"default", "research"} <= {v["vault_id"] for v in vaults_list}

    # Switch the active vault via the X-Mnesis-Vault header (re-authorized per request).
    hdr = {"X-Mnesis-Vault": "research"}
    assert ec.get("/api/vaults", headers=hdr).json()["active_vault"] == "research"

    # Edit that vault's schema (entity types + predicates) — owner is allowed.
    put = ec.put("/api/vaults/research/config",
                 json={"entity_types": ["person", "org"], "predicates": ["employs", "uses"]},
                 headers={**ecsrf, **hdr})
    assert put.status_code == 200
    cfg = ec.get("/api/vaults/research/config", headers=hdr).json()
    assert "employs" in cfg["predicates"] and "org" in cfg["entity_types"]

    # Rename works; delete requires the confirm guard.
    assert ec.post("/api/vaults/research/rename", json={"name": "R2"}, headers=ecsrf).status_code == 200
    bad = ec.post("/api/vaults/research/delete", json={"confirm": "WRONG"}, headers=ecsrf)
    assert bad.status_code == 400 and bad.json()["reason"] == "confirm_mismatch"
    ok = ec.post("/api/vaults/research/delete", json={"confirm": "research"}, headers=ecsrf)
    assert ok.status_code == 200


def test_user_cannot_reach_another_users_vaults(app):
    c, csrf = _ready_admin(app)
    # Two users, each with their own vault.
    for who in ("frank", "gina"):
        created = c.post("/api/admin/users", json={"username": who, "role": "user"}, headers=csrf).json()
        account.change_own_password(who, who, created["initial_password"], f"{who}-real-passphrase-1")

    fc = _client(app)
    _login(fc, "frank", "frank-real-passphrase-1")
    fcsrf = {"X-CSRF-Token": fc.cookies["mnesis_csrf"]}
    fc.post("/api/vaults", json={"vault_id": "frank-priv"}, headers=fcsrf)

    gc = _client(app)
    _login(gc, "gina", "gina-real-passphrase-1")
    ghdr = {"X-Mnesis-Vault": "frank-priv"}
    # gina (tenant "gina") can never even NAME frank's vault → selection re-auth denies (403).
    assert gc.get("/api/vaults/frank-priv/config", headers=ghdr).status_code in (403, 404)
    assert gc.get("/api/pages", headers=ghdr).status_code == 403  # ungranted vault → denied
    # gina's own vault listing never contains frank's vault.
    assert "frank-priv" not in {v["vault_id"] for v in gc.get("/api/vaults").json()["vaults"]}
