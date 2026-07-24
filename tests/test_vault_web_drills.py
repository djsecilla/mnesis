"""V9 — the end-to-end vault DRILLS on the WEB surface.

Ties the vault feature together and verifies the guardrails: a user creates a second vault,
switches to it and sees an INDEPENDENT knowledge base, switches back with no bleed; search
(and the graph-augmented query) follow the active vault; per-vault config edits stay scoped
to that vault; rename is display-only, delete is permanent and refused for the last vault; a
first-login (must_change) principal cannot reach vault management; a newly created user has
its default vault active on first login; an admin gets no extra vault visibility; and every
vault lifecycle action from the surface is audited.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from mnesis import account, admin, config, graph, search, store, tenancy, webapi, webauth
from mnesis.store import Page

PW = "correct horse battery staple"


@pytest.fixture()
def app(monkeypatch):
    tmp = Path(tempfile.mkdtemp(prefix="mnesis-vaultwd-"))
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


def _admin(app) -> tuple[TestClient, dict]:
    c = TestClient(app)
    _login(c, "admin", PW, tenant=config.DEFAULT_TENANT_ID)
    c.post("/api/auth/change-password",
           json={"current_password": PW, "new_password": "admin-real-passphrase-1"}, headers=_csrf(c))
    return c, _csrf(c)


def _user_client(app, ac: TestClient, csrf: dict, name: str) -> tuple[TestClient, dict]:
    created = ac.post("/api/admin/users", json={"username": name, "role": "user"}, headers=csrf).json()
    account.change_own_password(name, name, created["initial_password"], f"{name}-real-passphrase-1")
    c = TestClient(app)
    _login(c, name, f"{name}-real-passphrase-1")
    return c, _csrf(c)


def _seed(tenant: str, vault: str, page_id: str, title: str, tags: list[str]) -> None:
    with tenancy.use(tenancy.context_for(tenant, vault)):
        store.write_page(Page(id=page_id, title=title, body=title, tags=tags))
        search.rebuild()
        graph.rebuild_graph()


def _hdr(vault: str) -> dict:
    return {"X-Mnesis-Vault": vault}


def _vault_audit(app) -> list[dict]:
    path = config.vault_audit_path()
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()] if path.is_file() else []


# ── DRILL 1: a second vault is an independent KB; switching back has no bleed ─


def test_drill_second_vault_is_independent_and_no_bleed(app):
    ac, csrf = _admin(app)
    alice, acsrf = _user_client(app, ac, csrf, "alice")
    alice.post("/api/vaults", json={"name": "Beta"}, headers=acsrf)

    _seed("alice", "default", "alpha-fact", "Alpha fact about Redis", ["library:redis"])
    _seed("alice", "beta", "beta-fact", "Beta fact about Kafka", ["library:kafka"])

    def page_ids(vault: str) -> set[str]:
        return {p["id"] for p in alice.get("/api/pages", headers=_hdr(vault)).json()["pages"]}

    # Switching to Beta shows ONLY Beta's page; the new vault is otherwise independent.
    assert page_ids("beta") == {"beta-fact"}
    # Switching back to default restores Alpha with no bleed from Beta.
    assert page_ids("default") == {"alpha-fact"}


# ── DRILL 2: search + the graph-augmented query follow the active vault ─────


def test_drill_search_and_graph_follow_active_vault(app):
    ac, csrf = _admin(app)
    alice, acsrf = _user_client(app, ac, csrf, "alice")
    alice.post("/api/vaults", json={"name": "Beta"}, headers=acsrf)
    _seed("alice", "default", "alpha-redis", "Alpha uses Redis", ["library:redis"])
    _seed("alice", "beta", "beta-redis", "Beta uses Redis", ["library:redis"])

    def hit_ids(vault: str) -> set[str]:
        return {h["id"] for h in alice.get("/api/search", params={"q": "redis"}, headers=_hdr(vault)).json()["hits"]}

    assert "alpha-redis" in hit_ids("default") and "beta-redis" not in hit_ids("default")
    assert "beta-redis" in hit_ids("beta") and "alpha-redis" not in hit_ids("beta")

    # The graph is per-vault too: Beta's entity graph never carries Alpha's page node.
    beta_graph = alice.get("/api/graph", headers=_hdr("beta")).json()
    node_ids = {n.get("id") for n in beta_graph.get("nodes", [])}
    assert "alpha-redis" not in node_ids


# ── DRILL 3: per-vault config edits stay scoped to that vault ────────────────


def test_drill_config_edits_are_vault_scoped(app):
    ac, csrf = _admin(app)
    alice, acsrf = _user_client(app, ac, csrf, "alice")
    alice.post("/api/vaults", json={"name": "Beta"}, headers=acsrf)
    alice.post("/api/vaults", json={"name": "Gamma"}, headers=acsrf)

    # Edit ONLY beta's schema.
    put = alice.put("/api/vaults/beta/config",
                    json={"entity_types": ["person", "org"], "predicates": ["employs", "uses"]}, headers=acsrf)
    assert put.status_code == 200

    beta_cfg = alice.get("/api/vaults/beta/config").json()
    assert "org" in beta_cfg["entity_types"] and "employs" in beta_cfg["predicates"]
    # Gamma is untouched — the edit did not bleed across vaults.
    gamma_cfg = alice.get("/api/vaults/gamma/config").json()
    assert "org" not in gamma_cfg["entity_types"] and "employs" not in gamma_cfg["predicates"]


# ── DRILL 4: rename is display-only; delete permanent + last-vault refused ──


def test_drill_rename_display_only_and_delete_last_refused(app):
    ac, csrf = _admin(app)
    alice, acsrf = _user_client(app, ac, csrf, "alice")
    alice.post("/api/vaults", json={"name": "Beta"}, headers=acsrf)

    root = config.DATA_ROOT / config.TENANTS_DIRNAME / "alice" / config.VAULTS_DIRNAME / "beta"
    r = alice.patch("/api/vaults/beta", json={"name": "Renamed"}, headers=acsrf)
    assert r.status_code == 200 and r.json()["vault_id"] == "beta" and root.is_dir()

    ok = alice.delete("/api/vaults/beta", params={"confirm": "beta"}, headers=acsrf)
    assert ok.status_code == 200 and not root.exists()
    # Only default remains → deleting it is refused (no-lockout).
    last = alice.delete("/api/vaults/default", params={"confirm": "default"}, headers=acsrf)
    assert last.status_code == 409 and last.json()["reason"] == "last_vault"


# ── DRILL 5: a user cannot switch to or manage another principal's vault ────


def test_drill_cross_principal_vault_denied_on_every_endpoint(app):
    ac, csrf = _admin(app)
    alice, acsrf = _user_client(app, ac, csrf, "alice")
    bob, bcsrf = _user_client(app, ac, csrf, "bob")
    alice.post("/api/vaults", json={"name": "Research"}, headers=acsrf)

    assert bob.post("/api/vaults/research/activate", json={}, headers=bcsrf).status_code == 404
    assert bob.patch("/api/vaults/research", json={"name": "x"}, headers=bcsrf).status_code == 404
    assert bob.delete("/api/vaults/research", params={"confirm": "research"}, headers=bcsrf).status_code == 404
    assert bob.get("/api/vaults/research/config").status_code in (403, 404)
    assert "research" not in {v["vault_id"] for v in bob.get("/api/vaults").json()["vaults"]}


def test_drill_admin_has_no_extra_vault_visibility(app):
    ac, csrf = _admin(app)
    carol, ccsrf = _user_client(app, ac, csrf, "carol")
    carol.post("/api/vaults", json={"name": "Research"}, headers=ccsrf)
    assert "research" not in {v["vault_id"] for v in ac.get("/api/vaults").json()["vaults"]}
    assert ac.post("/api/vaults/research/activate", json={}, headers=csrf).status_code == 404


# ── DRILL 6: first-login (restricted) cannot reach vault management ─────────


def test_drill_first_login_cannot_reach_vaults(app):
    ac, csrf = _admin(app)
    ac.post("/api/admin/users", json={"username": "dave", "role": "user"}, headers=csrf)

    dc = TestClient(app)
    login = _login(dc, "dave", ac.post(  # dave's one-time credential (still must-change)
        "/api/admin/users/dave/reset-password", json={}, headers=csrf).json()["initial_password"], tenant="dave")
    assert login["must_change_password"] is True
    dcsrf = _csrf(dc)

    # Restricted session is denied on every vault endpoint (server-side, not just the UI).
    assert dc.get("/api/vaults").status_code == 403
    assert dc.post("/api/vaults", json={"name": "x"}, headers=dcsrf).status_code == 403
    assert dc.patch("/api/vaults/default", json={"name": "x"}, headers=dcsrf).status_code == 403
    assert dc.post("/api/vaults/default/activate", json={}, headers=dcsrf).status_code == 403
    assert dc.get("/api/vaults/default/config").status_code == 403


# ── DRILL 7: a newly created user has its default vault active on first use ──


def test_drill_new_user_has_active_default_vault(app):
    ac, csrf = _admin(app)
    erin, ecsrf = _user_client(app, ac, csrf, "erin")  # past first-login
    body = erin.get("/api/vaults").json()
    assert body["active_vault"] == "default"
    assert {v["vault_id"] for v in body["vaults"]} == {"default"}
    assert next(v for v in body["vaults"] if v["vault_id"] == "default")["is_active"] is True


# ── DRILL 8: vault lifecycle actions from the surface are audited ───────────


def test_drill_vault_lifecycle_is_audited(app):
    ac, csrf = _admin(app)
    alice, acsrf = _user_client(app, ac, csrf, "alice")
    alice.post("/api/vaults", json={"name": "Research"}, headers=acsrf)
    alice.patch("/api/vaults/research", json={"name": "Renamed"}, headers=acsrf)
    alice.put("/api/vaults/research/config", json={"entity_types": ["person"]}, headers=acsrf)
    alice.post("/api/vaults/research/activate", json={}, headers=acsrf)
    alice.delete("/api/vaults/research", params={"confirm": "research"}, headers=acsrf)

    events = [e for e in _vault_audit(app) if e.get("actor") == "alice" and e.get("vault_id") == "research"]
    actions = {e["action"] for e in events}
    assert {"create", "rename", "set_config", "activate", "delete"} <= actions
    assert all(e.get("tenant_id") == "alice" for e in events)
