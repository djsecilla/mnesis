"""R9 — the admin user-management DRILLS on the WEB surface, end to end.

Complements the CLI drills (`test_role_drills.py`) and the endpoint units
(`test_admin_user_api.py` / `test_admin_user_ui.py`) by verifying the whole web flow and
the R9 guardrails together: the admin area is unreachable until the forced first-login
change clears; an admin then performs full CRUD; a user sees no admin role and is denied at
the API even if forced; create provisions a tenant + default vault + forced first-login;
delete removes the user's data behind a typed confirmation and is audited; last-admin and
no-self-role-change hold; the read-only activity feed surfaces actions without secrets; and
an admin still cannot read another user's vault data (isolation unchanged).
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from mnesis import account, admin, config, ingest, tenancy, webapi, webauth

PW = "correct horse battery staple"


@pytest.fixture()
def app(monkeypatch):
    tmp = Path(tempfile.mkdtemp(prefix="mnesis-admindrills-"))
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


def _login(c: TestClient, user: str, pw: str, *, tenant: str | None = None) -> dict:
    r = c.post("/api/auth/login", json={"tenant_id": tenant or user, "username": user, "password": pw})
    assert r.status_code == 200, r.text
    return r.json()


def _csrf(c: TestClient) -> dict:
    return {"X-CSRF-Token": c.cookies["mnesis_csrf"]}


def _ready_admin(app) -> tuple[TestClient, dict]:
    c = TestClient(app)
    _login(c, "admin", PW, tenant=config.DEFAULT_TENANT_ID)
    c.post("/api/auth/change-password",
           json={"current_password": PW, "new_password": "admin-real-passphrase-1"}, headers=_csrf(c))
    return c, _csrf(c)


def _make_user(c: TestClient, csrf: dict, name: str, role: str = "user") -> str:
    created = c.post("/api/admin/users", json={"username": name, "role": role}, headers=csrf).json()
    real = f"{name}-real-passphrase-1"
    account.change_own_password(name, name, created["initial_password"], real)
    return real


def _audit(app) -> list[dict]:
    path = config.DATA_ROOT / config.AUTH_AUDIT_FILENAME
    if not path.is_file():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


# ── DRILL 1: the admin area is unreachable until first-login change clears ───


def test_drill_admin_area_gated_by_first_login(app):
    c = TestClient(app)
    login = _login(c, "admin", PW, tenant=config.DEFAULT_TENANT_ID)
    assert login["must_change_password"] is True
    csrf = _csrf(c)

    # While restricted, the Users area (and its audit feed) is denied — like everything else.
    assert c.get("/api/admin/users").status_code == 403
    assert c.get("/api/admin/audit").status_code == 403
    assert c.post("/api/admin/users", json={"username": "x", "role": "user"}, headers=csrf).status_code == 403

    # Clearing the forced change unlocks it (session rotated).
    c.post("/api/auth/change-password",
           json={"current_password": PW, "new_password": "admin-real-passphrase-1"}, headers=csrf)
    assert c.get("/api/admin/users").status_code == 200
    assert c.get("/api/admin/audit").status_code == 200


# ── DRILL 2: admin performs full CRUD ───────────────────────────────────────


def test_drill_admin_full_crud(app):
    c, csrf = _ready_admin(app)
    assert c.post("/api/admin/users", json={"username": "carol", "role": "user"}, headers=csrf).status_code == 201
    assert "carol" in {u["username"] for u in c.get("/api/admin/users").json()["users"]}
    assert c.patch("/api/admin/users/carol", json={"role": "admin"}, headers=csrf).status_code == 200
    assert c.patch("/api/admin/users/carol", json={"role": "user"}, headers=csrf).status_code == 200
    assert c.patch("/api/admin/users/carol", json={"status": "inactive"}, headers=csrf).status_code == 200
    assert c.patch("/api/admin/users/carol", json={"status": "active"}, headers=csrf).status_code == 200
    assert c.post("/api/admin/users/carol/reset-password", json={}, headers=csrf).status_code == 200
    assert c.post("/api/admin/users/carol/revoke-credentials", json={}, headers=csrf).status_code == 200
    assert c.delete("/api/admin/users/carol", params={"confirm": "carol"}, headers=csrf).status_code == 200


# ── DRILL 3: a user has no admin role and is denied at the API even if forced ──


def test_drill_user_has_no_admin_and_is_denied(app):
    ac, csrf = _ready_admin(app)
    _make_user(ac, csrf, "dan")
    dc = TestClient(app)
    _login(dc, "dan", "dan-real-passphrase-1")
    d = _csrf(dc)

    assert "admin" not in dc.get("/api/auth/session").json()["roles"]     # nav hidden
    assert dc.get("/api/admin/users").status_code == 403
    assert dc.get("/api/admin/audit").status_code == 403
    assert dc.post("/api/admin/users", json={"username": "x", "role": "user"}, headers=d).status_code == 403
    assert dc.patch("/api/admin/users/admin", json={"role": "user"}, headers=d).status_code == 403
    assert dc.delete("/api/admin/users/admin", params={"confirm": "admin"}, headers=d).status_code == 403


# ── DRILL 4: create provisions tenant + default vault + forced first-login ──


def test_drill_create_provisions_tenant_vault_forced_change(app):
    c, csrf = _ready_admin(app)
    created = c.post("/api/admin/users", json={"username": "erin", "role": "user"}, headers=csrf).json()
    assert created["tenant_id"] == "erin" and created["vault_id"] == config.DEFAULT_VAULT_ID
    assert created["must_change_password"] is True
    vctx = tenancy.context_for("erin", config.DEFAULT_VAULT_ID, data_root=config.DATA_ROOT)
    assert vctx.root_path.exists() and vctx.pages_dir.exists()


# ── DRILL 5: delete removes the user's data behind confirmation, and is audited ──


def test_drill_delete_removes_data_and_audits(app):
    c, csrf = _ready_admin(app)
    c.post("/api/admin/users", json={"username": "fred", "role": "user"}, headers=csrf)
    root = config.DATA_ROOT / config.TENANTS_DIRNAME / "fred"
    assert root.is_dir()

    assert c.delete("/api/admin/users/fred", headers=csrf).status_code == 400            # needs confirmation
    assert c.delete("/api/admin/users/fred", params={"confirm": "fred"}, headers=csrf).status_code == 200
    assert not root.exists()                                                             # data removed

    ev = [e for e in _audit(app) if e["event"] == "user_deleted" and e.get("principal_id") == "fred"]
    assert ev and ev[-1]["actor"] == "admin"


# ── DRILL 6: last-admin protection + no self-role-change (web surface) ───────


def test_drill_last_admin_and_no_self_role_change(app):
    c, csrf = _ready_admin(app)  # `admin` is the sole admin
    self_role = c.patch("/api/admin/users/admin", json={"role": "user"}, headers=csrf)
    assert self_role.status_code == 403 and self_role.json()["reason"] == "self_role_change"
    last = c.delete("/api/admin/users/admin", params={"confirm": "admin"}, headers=csrf)
    assert last.status_code == 403 and last.json()["reason"] == "last_admin"
    deact = c.patch("/api/admin/users/admin", json={"status": "inactive"}, headers=csrf)
    assert deact.status_code == 403 and deact.json()["reason"] == "last_admin"


# ── DRILL 7: the read-only activity feed surfaces actions, without secrets ──


def test_drill_audit_feed_scoped_and_secret_free(app):
    c, csrf = _ready_admin(app)
    secret = c.post("/api/admin/users", json={"username": "gina", "role": "user"}, headers=csrf).json()["initial_password"]
    c.patch("/api/admin/users/gina", json={"role": "admin"}, headers=csrf)
    c.patch("/api/admin/users/gina", json={"role": "user"}, headers=csrf)

    feed = c.get("/api/admin/audit").json()["events"]
    events = {e["event"] for e in feed}
    assert {"user_created", "user_role_assigned"} <= events
    assert all(e.get("actor") == "admin" for e in feed)                # scoped to this admin
    blob = json.dumps(feed)
    assert secret not in blob
    assert not any(k in e for e in feed for k in ("initial_password", "secret_hash", "password"))


# ── DRILL 8: managing accounts is NOT data access — isolation unchanged ─────


def test_drill_admin_cannot_read_another_users_vault_data(app):
    c, csrf = _ready_admin(app)
    c.post("/api/admin/users", json={"username": "hana", "role": "user"}, headers=csrf)

    # Put a page in hana's OWN vault (her tenant), directly through her store.
    hana_ctx = tenancy.open_tenant("hana")
    with tenancy.use(hana_ctx):
        hana_page = ingest.ingest_source("Hana's private note about penguins.", "hana-note")

    # The admin (tenant `default`) can manage hana's ACCOUNT but never see her DATA.
    admin_pages = {p["id"] for p in c.get("/api/pages").json()["pages"]}
    assert hana_page.id not in admin_pages
    assert c.get(f"/api/pages/{hana_page.id}").status_code == 404
