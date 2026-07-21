"""R8 — the admin Users (Administration) area in the Web UI.

The SPA half is a thin client of the R7 endpoints, so this suite has two parts:

  * **UI wiring** (source-level) — the admin-only nav entry, the ``/admin/users`` route +
    client guard, the CRUD API layer, the one-time-credential "won't see again" notice, and
    the typed delete confirmation all exist and are gated on the *server-resolved* role.
  * **The contract the screens rely on** (integration, no JS runtime needed) — the session
    role gates the nav (server truth); a non-admin is denied by the API even if it navigates
    straight to the endpoints; the one-time credential is shown once; delete needs the typed
    confirmation; and the service safety errors (last-admin, self-role-change) surface
    verbatim. Security rests on the server, not the client guard.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from mnesis import account, admin, config, webapi, webauth

REPO = Path(__file__).resolve().parents[1]
UI = REPO / "ui" / "src"
PW = "correct horse battery staple"


# ── Part 1: the visible half is wired (admin-gated in the source) ───────────


def _read(rel: str) -> str:
    return (UI / rel).read_text(encoding="utf-8")


def test_admin_users_screen_and_nav_exist_and_are_role_gated():
    # The screen exists, with the create form, the one-time-credential notice, and the
    # typed delete confirmation.
    screen = _read("routes/AdminUsersPage.tsx")
    assert "Create user" in screen
    assert "you won’t see it again" in screen.lower() or "won’t see it again" in screen
    assert "Type" in screen and "to confirm" in screen          # typed delete confirmation
    assert "confirmName !== user.username" in screen             # delete disabled until typed

    # The route is registered AND guarded for UX (redirect a non-admin who navigates in).
    app_tsx = _read("App.tsx")
    assert "/admin/users" in app_tsx and "AdminRoute" in app_tsx
    assert "isAdmin" in app_tsx

    # The nav entry is rendered only for an admin session (role from the server).
    shell = _read("components/Shell.tsx")
    assert "/admin/users" in shell and "isAdmin" in shell

    # The role is the server-resolved session role, never a client guess.
    ctx = _read("auth/AuthContext.tsx")
    assert 'session.roles.includes("admin")' in ctx


def test_admin_api_layer_calls_the_r7_endpoints():
    ep = _read("api/endpoints.ts")
    for fn in (
        "listAdminUsers", "createAdminUser", "patchAdminUser",
        "deleteAdminUser", "resetAdminUserPassword", "revokeAdminUserCredentials",
    ):
        assert fn in ep, f"missing admin endpoint wrapper {fn}"
    assert "/admin/users" in ep


# ── Part 2: the server contract the screens depend on ───────────────────────


@pytest.fixture()
def app(monkeypatch):
    tmp = Path(tempfile.mkdtemp(prefix="mnesis-adminui-"))
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


def test_session_role_gates_the_nav_entry(app):
    """The nav is shown/hidden from the server-resolved role — assert both sides."""
    ac, csrf = _ready_admin(app)
    assert "admin" in ac.get("/api/auth/session").json()["roles"]     # admin → nav shown

    _make_user(ac, csrf, "carol")
    uc = TestClient(app)
    _login(uc, "carol", "carol-real-passphrase-1")
    assert "admin" not in uc.get("/api/auth/session").json()["roles"]  # user → nav hidden


def test_non_admin_denied_by_api_even_if_forced(app):
    """The client route guard is UX; a non-admin forced to the endpoints is denied server-side."""
    ac, csrf = _ready_admin(app)
    _make_user(ac, csrf, "dan")
    dc = TestClient(app)
    _login(dc, "dan", "dan-real-passphrase-1")
    d = _csrf(dc)
    assert dc.get("/api/admin/users").status_code == 403
    assert dc.post("/api/admin/users", json={"username": "x", "role": "user"}, headers=d).status_code == 403
    assert dc.patch("/api/admin/users/admin", json={"role": "user"}, headers=d).status_code == 403
    assert dc.delete("/api/admin/users/admin", params={"confirm": "admin"}, headers=d).status_code == 403


def test_create_shows_one_time_credential_once(app):
    ac, csrf = _ready_admin(app)
    created = ac.post("/api/admin/users", json={"username": "erin", "role": "admin"}, headers=csrf).json()
    assert created["role"] == "admin" and created["initial_password"] and created["must_change_password"] is True
    # The credential is never returned again (the list carries no secret).
    listed = {u["username"]: u for u in ac.get("/api/admin/users").json()["users"]}
    assert "erin" in listed and "initial_password" not in listed["erin"]
    assert "created" in listed["erin"]  # the R8 "Created" column has real data


def test_delete_requires_typed_confirmation(app):
    ac, csrf = _ready_admin(app)
    ac.post("/api/admin/users", json={"username": "fred", "role": "user"}, headers=csrf)
    # No confirmation / a wrong one is refused (this is what the typed field guards).
    assert ac.delete("/api/admin/users/fred", headers=csrf).status_code == 400
    assert ac.delete("/api/admin/users/fred", params={"confirm": "nope"}, headers=csrf).json()["reason"] == "confirm_mismatch"
    # The exact username confirms.
    assert ac.delete("/api/admin/users/fred", params={"confirm": "fred"}, headers=csrf).status_code == 200


def test_service_safety_errors_render_clearly(app):
    ac, csrf = _ready_admin(app)  # `admin` is the sole admin
    # self-role-change and last-admin deletion come back as clear, typed messages.
    self_role = ac.patch("/api/admin/users/admin", json={"role": "user"}, headers=csrf)
    assert self_role.status_code == 403
    assert self_role.json()["reason"] == "self_role_change" and self_role.json()["message"]

    last = ac.delete("/api/admin/users/admin", params={"confirm": "admin"}, headers=csrf)
    assert last.status_code == 403
    assert last.json()["reason"] == "last_admin" and last.json()["message"]
