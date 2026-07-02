"""IAM5 — web authentication & authorization (retire the injected token).

The browser no longer carries a shared injected bearer token. It logs in with a real
user (IAM2 local provider), receives an httpOnly session cookie (IAM3), and every
``/api`` request + SSE stream is authorized server-side by the PDP (IAM4):

  - an unauthenticated request is refused (401);
  - login issues a session and access is scoped to the user's tenant/visibility/role;
  - a state-changing request without a CSRF token is refused (403);
  - logout invalidates the session immediately;
  - a member cannot hit an admin endpoint (403);
  - the injected-token path is gone — a bearer token no longer authenticates ``/api``.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from mnesis import config, providers, search, store, tenancy, webapi, webauth
from mnesis.store import Page

ADMIN_PW = "correct horse battery staple"
ALICE_PW = "alice-strong-passphrase-1"
ROB_PW = "rob-strong-passphrase-1"


@pytest.fixture()
def app(monkeypatch):
    """A standalone /api app (real webapi routes + IAM5 web-auth) with three users and
    a shared + a private page seeded in the default tenant."""
    tmp = Path(tempfile.mkdtemp(prefix="mnesis-webauth-"))
    monkeypatch.setattr(config, "DATA_ROOT", tmp / "data")
    monkeypatch.setattr(config, "MNESIS_LLM_STUB", True)
    monkeypatch.setattr(config, "MNESIS_WEB_COOKIE_SECURE", False)  # TestClient uses http

    ctx = tenancy.open_tenant(config.DEFAULT_TENANT_ID)
    tok = tenancy.bind(ctx)

    prov = providers.LocalPasswordProvider()
    prov.register(config.DEFAULT_TENANT_ID, "admin", "admin", ADMIN_PW)
    prov.register(config.DEFAULT_TENANT_ID, "alice", "member", ALICE_PW)
    prov.register(config.DEFAULT_TENANT_ID, "rob", "readonly", ROB_PW)

    # A shared page everyone sees, and a private page only 'bob' owns.
    store.write_page(Page(id="shared1", title="Shared fact", body="Everyone can read this.",
                          visibility="shared"))
    store.write_page(Page(id="secretbob", title="Bob's private note", body="Only bob.",
                          owner_principal="bob", visibility="private"))
    search.rebuild()

    application = Starlette()
    webapi.mount_api(application)
    webauth.install(application)

    yield application
    tenancy.unbind(tok)
    shutil.rmtree(tmp, ignore_errors=True)


def _login(client: TestClient, user: str, pw: str) -> dict:
    r = client.post("/api/auth/login", json={"username": user, "password": pw})
    assert r.status_code == 200, r.text
    return {"X-CSRF-Token": client.cookies["mnesis_csrf"]}


# ── unauthenticated is refused ──────────────────────────────────────────────


def test_unauthenticated_request_refused(app):
    c = TestClient(app)
    assert c.get("/api/pages").status_code == 401
    assert c.get("/api/pages/shared1").status_code == 401
    assert c.post("/api/ingest/preview", json={"text": "x"}).status_code == 401


# ── login issues a session, scoped to visibility/role ───────────────────────


def test_login_scopes_access_to_visibility(app):
    # A member sees shared pages but not another principal's private page.
    alice = TestClient(app)
    _login(alice, "alice", ALICE_PW)
    ids = {p["id"] for p in alice.get("/api/pages").json()["pages"]}
    assert "shared1" in ids and "secretbob" not in ids
    assert alice.get("/api/pages/secretbob").status_code == 404  # absent, no leak

    # The session endpoint reports the resolved principal (server-side identity).
    me = alice.get("/api/auth/session").json()
    assert me["principal_id"] == "alice" and me["roles"] == ["member"]


def test_login_sets_httponly_session_cookie(app):
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"username": "alice", "password": ALICE_PW})
    assert r.status_code == 200
    set_cookie = " ".join(r.headers.get_list("set-cookie")).lower()
    assert "mnesis_session=" in set_cookie and "httponly" in set_cookie
    assert "mnesis_csrf=" in set_cookie
    # The raw session is never returned in the response body.
    assert "mnesis_session" not in r.text


def test_wrong_password_denied(app):
    c = TestClient(app)
    assert c.post("/api/auth/login", json={"username": "alice", "password": "nope"}).status_code == 401


# ── CSRF on state-changing requests ─────────────────────────────────────────


def test_state_change_without_csrf_refused(app):
    alice = TestClient(app)
    csrf = _login(alice, "alice", ALICE_PW)
    # No CSRF header → 403 even with a valid session cookie.
    assert alice.post("/api/ingest/preview", json={"text": "hello"}).status_code == 403
    # With the CSRF header → allowed (member may write).
    assert alice.post("/api/ingest/preview", json={"text": "hello"}, headers=csrf).status_code == 200


# ── logout invalidates the session ──────────────────────────────────────────


def test_logout_invalidates_session(app):
    alice = TestClient(app)
    csrf = _login(alice, "alice", ALICE_PW)
    assert alice.get("/api/pages").status_code == 200
    assert alice.post("/api/auth/logout", headers=csrf).status_code == 200
    # The (revoked) session no longer authenticates — immediate, server-side.
    assert alice.get("/api/pages").status_code == 401


# ── role: a member cannot hit an admin endpoint ─────────────────────────────


def test_member_cannot_hit_admin_endpoint(app):
    alice = TestClient(app)
    _login(alice, "alice", ALICE_PW)
    r = alice.get("/api/admin/credentials")
    assert r.status_code == 403 and r.json()["reason"] == "insufficient_role"

    admin = TestClient(app)
    _login(admin, "admin", ADMIN_PW)
    assert admin.get("/api/admin/credentials").status_code == 200


def test_readonly_cannot_write(app):
    rob = TestClient(app)
    csrf = _login(rob, "rob", ROB_PW)
    assert rob.get("/api/pages").status_code == 200  # reads fine
    r = rob.post("/api/ingest/preview", json={"text": "x"}, headers=csrf)
    assert r.status_code == 403 and r.json()["reason"] == "insufficient_role"


# ── the injected token is gone ──────────────────────────────────────────────


def test_injected_bearer_token_no_longer_authenticates(app, monkeypatch):
    # Even with a configured MCP token, presenting it as a bearer to /api is refused —
    # the web injected-token path is retired; only a session authenticates the browser.
    monkeypatch.setattr(config, "MNESIS_MCP_TOKEN", "the-old-injected-token")
    c = TestClient(app)
    r = c.get("/api/pages", headers={"Authorization": "Bearer the-old-injected-token"})
    assert r.status_code == 401
