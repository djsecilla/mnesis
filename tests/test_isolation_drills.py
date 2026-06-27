"""T7 — cross-tenant isolation drills + lifecycle, admin boundary, and quotas.

The end-to-end drills: provision tenants A and B; A can never read/search/graph/
receive B's data; a forged tenant id never crosses; a private resource never leaks
within a tenant; the admin boundary holds (only the system-admin manages tenants);
suspend denies access while retaining data; delete removes everything and is
audited; quotas fail closed; and the `default`-tenant migration preserves prior
behaviour. (Per-surface transport isolation is also covered by
tests/test_surface_isolation.py and tests/test_agent_tenancy.py.)
"""

from __future__ import annotations

import pytest

from mnesis import admin, auth, config, graph, ingest, mcp_server, quotas, search, store, tenancy
from mnesis.store import Page


@pytest.fixture()
def system(tmp_path, monkeypatch):
    """A data root with a bootstrapped system admin and two provisioned tenants
    (A, B), each seeded with a shared page + a private page. Returns a namespace."""
    monkeypatch.setattr(config, "DATA_ROOT", tmp_path / "data", raising=False)
    monkeypatch.setattr(config, "MNESIS_LLM_STUB", True, raising=False)

    raw_admin, _ = admin.bootstrap_admin("root")
    sysp = auth.resolve_admin(raw_admin)
    a = admin.provision_tenant("alpha", admin=sysp, admin_principal="ann")
    b = admin.provision_tenant("beta", admin=sysp, admin_principal="bob")
    creds = auth.CredentialStore()
    # A second principal in alpha (a member) for the within-tenant visibility drill.
    a_member, _ = creds.issue("alpha", "amy", "member")

    def seed(token, team):
        ctx, principal = auth.resolve_principal(token)
        with auth.authenticated(token):
            store.write_page(Page(
                id="redis-cache", title=f"{team} uses Redis for caching",
                body=f"{team} caches in Redis.", owner_principal=principal.principal_id,
                visibility="shared", tags=[f"project:{team.lower()}", "library:redis"],
                relations=[{"s": f"project:{team.lower()}", "p": "uses", "o": "library:redis"}],
            ))
            store.write_page(Page(
                id=f"secret-{team.lower()}", title=f"{team} private secret",
                body=f"{team}'s confidential note.", owner_principal=principal.principal_id,
                visibility="private", tags=[f"secret:{team.lower()}"],
            ))
            search.rebuild()
            graph.rebuild_graph()

    seed(a["token"], "Alpha")
    seed(b["token"], "Beta")

    return type("Sys", (), {
        "sysp": sysp, "raw_admin": raw_admin, "tok_a": a["token"], "tok_b": b["token"],
        "a_member": a_member, "data_root": tmp_path / "data",
    })


# ── cross-tenant: A can never read/search/graph/receive B's data ────────────


def test_a_never_sees_bs_data_by_any_query_path(system):
    with auth.authenticated(system.tok_a):
        assert {h.id for h in search.search("redis")} == {"redis-cache"}
        # The shared id resolves to ALPHA's own content, never Beta's.
        assert "Alpha uses Redis" in mcp_server.mnesis_get("redis-cache")
        assert "Beta" not in mnesis_list_text()
        # Beta's unique entity/secret is unreachable.
        assert graph.entity("project:beta") is None
        assert "no such page" in mcp_server.mnesis_get("secret-beta").lower()
    with auth.authenticated(system.tok_b):
        assert "Beta uses Redis" in mcp_server.mnesis_get("redis-cache")
        assert graph.entity("project:alpha") is None
        assert "no such page" in mcp_server.mnesis_get("secret-alpha").lower()


def mnesis_list_text():
    return mcp_server.mnesis_list()


def test_forged_tenant_in_a_request_never_crosses(system):
    # resolve_principal takes the tenant ONLY from the credential — there is no
    # parameter through which a caller could supply one. The credential wins.
    ctx_a, _ = auth.resolve_principal(system.tok_a)
    ctx_b, _ = auth.resolve_principal(system.tok_b)
    assert ctx_a.tenant_id == "alpha" and ctx_b.tenant_id == "beta"
    assert ctx_a.root_path != ctx_b.root_path


def test_private_resource_never_leaks_within_a_tenant(system):
    # Alpha's private secret (owned by ann) is invisible to another alpha principal.
    with auth.authenticated(system.a_member):  # amy, a different alpha member
        assert {h.id for h in search.search("secret")} == set()
        assert "no such page" in mcp_server.mnesis_get("secret-alpha").lower()
        assert {h.id for h in search.search("redis")} == {"redis-cache"}  # shared still visible


# ── admin boundary: only the system-admin manages tenants ───────────────────


def test_only_the_system_admin_can_manage_tenants(system):
    _ctx, alpha_admin = auth.resolve_principal(system.tok_a)  # alpha's own admin role
    assert alpha_admin.role == "admin"  # admin WITHIN alpha — but not a system admin

    for op in (
        lambda: admin.list_tenants(admin=alpha_admin),
        lambda: admin.provision_tenant("gamma", admin=alpha_admin),
        lambda: admin.suspend_tenant("beta", admin=alpha_admin),
        lambda: admin.delete_tenant("beta", admin=alpha_admin, confirm="beta"),
    ):
        with pytest.raises(admin.AdminAccessError):
            op()
    # A tenant credential is also refused at the admin resolver itself.
    with pytest.raises(auth.InvalidCredential):
        auth.resolve_admin(system.tok_a)
    # The system admin can.
    assert {t.tenant_id for t in admin.list_tenants(admin=system.sysp)} == {"alpha", "beta"}


# ── suspend retains data; delete removes everything; both audited ───────────


def test_suspend_denies_access_but_retains_data(system):
    admin.suspend_tenant("alpha", admin=system.sysp)
    with pytest.raises(auth.InvalidCredential):
        auth.resolve_principal(system.tok_a)  # access denied while suspended
    # Data is retained on disk.
    ctx = tenancy.context_for("alpha")
    assert (ctx.pages_dir / "redis-cache.md").exists()
    # Resume restores access.
    admin.resume_tenant("alpha", admin=system.sysp)
    assert auth.resolve_principal(system.tok_a)[0].tenant_id == "alpha"


def test_delete_removes_everything_and_is_audited(system, tmp_path):
    ctx = tenancy.context_for("alpha")
    res = admin.delete_tenant(
        "alpha", admin=system.sysp, confirm="alpha",
        agent_state_base=tmp_path / "agent_state",
    )
    assert res["removed_root"] and res["credentials_removed"] >= 1
    assert not ctx.root_path.exists()                         # data gone
    assert auth.CredentialStore().list_for_tenant("alpha") == []  # credentials gone
    assert not tenancy.TenantRegistry().exists("alpha")       # registry record gone
    with pytest.raises(auth.InvalidCredential):
        auth.resolve_principal(system.tok_a)                  # credential no longer resolves
    # The lifecycle is audited in the SYSTEM log (which survives — outside the root).
    actions = [r["action"] for r in admin.SystemAuditLog().all()]
    assert "delete" in actions and "provision" in actions and "bootstrap_admin" in actions


def test_delete_is_guarded_by_confirmation(system):
    with pytest.raises(admin.AdminAccessError):
        admin.delete_tenant("alpha", admin=system.sysp, confirm="wrong")
    assert tenancy.context_for("alpha").root_path.exists()  # nothing removed


# ── quotas fail closed ──────────────────────────────────────────────────────


def test_page_quota_fails_closed(system):
    admin.set_quota("alpha", admin=system.sysp, max_pages=2)  # already at 2 pages
    with auth.authenticated(system.tok_a):
        with pytest.raises(quotas.QuotaExceeded):
            ingest.ingest_source("A brand new fact about penguins.", "peng")
        # The MCP tool surfaces it clearly rather than crashing.
        out = mcp_server.mnesis_ingest("Another new fact about whales.", "whale")
        assert "not ingested" in out and "quota" in out.lower()


def test_quota_does_not_cross_tenants(system):
    # Alpha at its limit does not stop Beta from writing.
    admin.set_quota("alpha", admin=system.sysp, max_pages=2)
    with auth.authenticated(system.tok_b):
        page = ingest.ingest_source("Beta adds a fresh fact about caching.", "beta-extra")
        assert page.id  # Beta wrote fine


# ── default-tenant migration preserves prior behaviour ──────────────────────


def test_default_tenant_still_works_transparently(tmp_path, monkeypatch):
    """A single-tenant deployment reaches its data as `default` with no admin/auth —
    legacy behaviour is preserved."""
    root = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_ROOT", root, raising=False)
    monkeypatch.setattr(config, "MNESIS_LLM_STUB", True, raising=False)
    ctx = tenancy.open_tenant("default")  # migrates legacy data if any; here a fresh tenant
    with tenancy.use(ctx):
        page = ingest.ingest_source("Project Atlas uses Redis for caching.", "atlas")
        search.rebuild()
        assert page.visibility == "shared" and page.owner_principal is None  # legacy, unowned
        assert [h.id for h in search.search("redis")] == [page.id]
