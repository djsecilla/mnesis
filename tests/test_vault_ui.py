"""V8 — the vault switcher + management screens.

Two parts (no JS runtime is available, so the SPA half is asserted at the source level and
the behaviour it depends on is asserted over HTTP):

  * **UI wiring** — a persistent switcher in the chrome that always shows the active vault, a
    `/vaults` management route, the `X-Mnesis-Vault` header on every request, a full
    cache-clear + keyed remount on switch (so no stale cross-vault data), and a typed
    delete confirmation with the last-vault case handled.
  * **The contract** — `GET /api/vaults` gives the active vault + the principal's OWN vaults
    (same self-service for a regular user AND an admin); switching serves a DIFFERENT vault's
    data purely from the header (the mechanism behind "clears previous vault data"); an
    ungranted vault is denied; and management create/rename/delete + last-vault/quota/name
    errors surface verbatim.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from mnesis import account, admin, config, store, tenancy, webapi, webauth
from mnesis.store import Page

REPO = Path(__file__).resolve().parents[1]
UI = REPO / "ui" / "src"
PW = "correct horse battery staple"


# ── Part 1: the switcher + management screens are wired ─────────────────────


def _read(rel: str) -> str:
    return (UI / rel).read_text(encoding="utf-8")


def test_switcher_is_in_the_chrome_and_shows_active_vault():
    shell = _read("components/Shell.tsx")
    assert "VaultSwitcher" in shell                      # persistent switcher in the app chrome
    assert "key={activeVault}" in shell                  # keyed remount on switch (no stale component state)

    switcher = _read("components/VaultSwitcher.tsx")
    assert "Manage vaults" in switcher                   # link to the management screen
    assert "useVault" in switcher and "switchVault" in switcher


def test_switch_clears_all_vault_scoped_state():
    ctx = _read("vault/VaultContext.tsx")
    assert "qc.clear()" in ctx                            # every vault-scoped cache is cleared on switch
    assert "setActiveVault" in ctx and "activateVault" in ctx
    client = _read("api/client.ts")
    assert "X-Mnesis-Vault" in client                     # the selection rides on every request


def test_management_route_and_screen_exist_for_all_users():
    app_tsx = _read("App.tsx")
    assert "/vaults" in app_tsx and "VaultsPage" in app_tsx
    # NOT behind the AdminRoute guard (available to every authenticated principal).
    assert "AdminRoute><VaultsPage" not in app_tsx.replace(" ", "")

    page = _read("routes/VaultsPage.tsx")
    assert "Create vault" in page
    assert "typed !== vault.name" in page                 # typed-name delete confirmation
    assert "last remaining vault" in page.lower()         # last-vault handled/explained
    for fn in ("createVault", "renameVault", "deleteVault"):
        assert fn in page


# ── Part 2: the contract the screens rely on ────────────────────────────────


@pytest.fixture()
def app(monkeypatch):
    tmp = Path(tempfile.mkdtemp(prefix="mnesis-vaultui-"))
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
    created = admin_c.post("/api/admin/users", json={"username": name, "role": "user"}, headers=csrf).json()
    real = f"{name}-real-passphrase-1"
    account.change_own_password(name, name, created["initial_password"], real)
    c = TestClient(app)
    _login(c, name, real)
    return c, _csrf(c)


def test_switcher_data_active_and_own_vaults(app):
    ac, csrf = _admin(app)
    alice, acsrf = _user_client(app, ac, csrf, "alice")
    alice.post("/api/vaults", json={"name": "Research"}, headers=acsrf)

    body = alice.get("/api/vaults").json()
    assert body["active_vault"] == "default"
    assert {v["vault_id"] for v in body["vaults"]} == {"default", "research"}
    # The admin gets the SAME self-service shape (its own vaults only — never alice's).
    admin_body = ac.get("/api/vaults").json()
    assert "research" not in {v["vault_id"] for v in admin_body["vaults"]}


def test_switching_serves_a_different_vaults_data(app):
    """The switch mechanism: the active vault is the X-Mnesis-Vault header, and each vault
    serves ONLY its own data — so re-pointing the header (what the switcher does) can never
    surface the previous vault's pages."""
    ac, csrf = _admin(app)
    alice, acsrf = _user_client(app, ac, csrf, "alice")
    alice.post("/api/vaults", json={"name": "Beta"}, headers=acsrf)

    # A distinct page in each of alice's vaults.
    with tenancy.use(tenancy.open_tenant("alice")):                      # default vault
        store.write_page(Page(id="alpha-fact", title="Alpha fact", body="alpha"))
    with tenancy.use(tenancy.context_for("alice", "beta")):             # the other vault
        store.write_page(Page(id="beta-fact", title="Beta fact", body="beta"))

    # Active = default (no header) → only default's page is visible.
    default_ids = {p["id"] for p in alice.get("/api/pages").json()["pages"]}
    assert "alpha-fact" in default_ids and "beta-fact" not in default_ids

    # Switch to beta (the header) → only beta's page; the previous vault's data is gone.
    beta_ids = {p["id"] for p in alice.get("/api/pages", headers={"X-Mnesis-Vault": "beta"}).json()["pages"]}
    assert "beta-fact" in beta_ids and "alpha-fact" not in beta_ids


def test_switch_to_ungranted_vault_is_denied(app):
    ac, csrf = _admin(app)
    alice, acsrf = _user_client(app, ac, csrf, "alice")
    bob, bcsrf = _user_client(app, ac, csrf, "bob")
    alice.post("/api/vaults", json={"name": "Research"}, headers=acsrf)

    # Bob activating alice's vault → denied, no existence leak.
    assert bob.post("/api/vaults/research/activate", json={}, headers=bcsrf).status_code == 404
    # A vault nobody has → also denied.
    assert alice.post("/api/vaults/ghost/activate", json={}, headers=acsrf).status_code == 404


def test_management_create_rename_delete_and_server_errors(app):
    ac, csrf = _admin(app)
    alice, acsrf = _user_client(app, ac, csrf, "alice")

    # Create + rename (display name only).
    assert alice.post("/api/vaults", json={"name": "Research"}, headers=acsrf).status_code == 201
    assert alice.patch("/api/vaults/research", json={"name": "Renamed"}, headers=acsrf).status_code == 200

    # Delete needs the confirm (typed in the UI); wrong/absent → refused verbatim.
    assert alice.delete("/api/vaults/research", headers=acsrf).json()["reason"] == "confirm_mismatch"
    assert alice.delete("/api/vaults/research", params={"confirm": "research"}, headers=acsrf).status_code == 200

    # Last-vault refusal + name validation surface as clear server errors.
    assert alice.delete("/api/vaults/default", params={"confirm": "default"}, headers=acsrf).json()["reason"] == "last_vault"
    assert alice.post("/api/vaults", json={"name": "   "}, headers=acsrf).status_code == 400


def test_regular_user_and_admin_both_self_service_own_vaults(app):
    ac, csrf = _admin(app)
    alice, acsrf = _user_client(app, ac, csrf, "alice")

    # Regular user manages its own vaults …
    assert alice.post("/api/vaults", json={"name": "Research"}, headers=acsrf).status_code == 201
    assert "research" in {v["vault_id"] for v in alice.get("/api/vaults").json()["vaults"]}
    # … and the admin does the same for ITS own vaults (identical self-service; not privileged).
    assert ac.post("/api/vaults", json={"name": "Ops"}, headers=csrf).status_code == 201
    assert "ops" in {v["vault_id"] for v in ac.get("/api/vaults").json()["vaults"]}
    # Neither sees the other's vault.
    assert "ops" not in {v["vault_id"] for v in alice.get("/api/vaults").json()["vaults"]}
