"""V6 — the vault isolation DRILLS + lifecycle/admin/quotas (CLAUDE.md §16 Vaults).

End-to-end verification that vaults are a complete, operationalized isolation unit:
  - a user with two vaults sees each in complete isolation (store/search/graph/state);
  - the two may carry different predicate/entity-type schemas;
  - selecting an ungranted vault is denied everywhere (fail closed);
  - lifecycle: create is permission- + quota-gated; rename/grant/revoke/delete are
    owner/tenant-admin gated and audited; deletion removes ALL of a vault's data;
  - per-vault quotas bind within the tenant;
  - the ``default``-vault migration preserves prior behaviour (single-vault transparency).
"""

from __future__ import annotations

import pytest

from mnesis import (
    authz,
    config,
    graph,
    identity,
    ingest,
    quotas,
    search,
    state,
    store,
    tenancy,
    vaults,
    vocab,
)
from mnesis.store import Page


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Tenant ``acme`` with member ``alice`` (owns vaults ``work`` + ``home`` of different
    schemas) and an ungranted ``locked`` (owned by bob). Returns (root, alice, bob)."""
    root = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_ROOT", root, raising=False)
    monkeypatch.setattr(config, "MNESIS_LLM_STUB", True, raising=False)
    tenancy.create_tenant("acme", data_root=root)

    alice = identity.Principal(principal_id="alice", tenant_id="acme", role="member")
    bob = identity.Principal(principal_id="bob", tenant_id="acme", role="member")

    vaults.create_vault(alice, "work", data_root=root,
                        schema=vocab.VaultConfig(entity_types=("person", "org", "project"),
                                                 predicates=("employs", "uses")))
    vaults.create_vault(alice, "home", data_root=root)  # default schema
    vaults.create_vault(bob, "locked", data_root=root)  # alice NOT granted

    _seed("acme", "work", root, [("w1", "Work: Acme employs Bob",
                                  [{"s": "org:acme", "p": "employs", "o": "person:bob"}])])
    _seed("acme", "home", root, [("h1", "Home: Atlas uses Redis",
                                  [{"s": "project:atlas", "p": "uses", "o": "library:redis"}])])
    return root, alice, bob


def _seed(tenant, vault, root, pages):
    ctx = tenancy.context_for(tenant, vault, data_root=root)
    with tenancy.use(ctx):
        for pid, title, rels in pages:
            store.write_page(Page(id=pid, title=title, body=f"{title}.",
                                  tags=["library:redis"] if vault == "home" else ["org:acme"],
                                  relations=rels))
        search.rebuild()
        graph.rebuild_graph()


# ── DRILL 1: two vaults in complete isolation ───────────────────────────────


def test_drill_two_vaults_are_completely_isolated(env):
    root, alice, bob = env
    work = authz.resolve_vault(alice, "work", data_root=root)
    home = authz.resolve_vault(alice, "home", data_root=root)
    with tenancy.use(work):
        assert {p.id for p in store.list_pages()} == {"w1"}
        assert {h.id for h in search.search("acme")} == {"w1"}
        assert {h.id for h in search.search("redis")} == set()   # home's topic, not here
    with tenancy.use(home):
        assert {p.id for p in store.list_pages()} == {"h1"}
        assert {h.id for h in search.search("redis")} == {"h1"}
        with pytest.raises(FileNotFoundError):
            store.read_page("w1")  # work's page is unreachable from home


# ── DRILL 2: the two carry different schemas ────────────────────────────────


def test_drill_vaults_carry_different_schemas(env):
    root, alice, bob = env
    with tenancy.use(authz.resolve_vault(alice, "work", data_root=root)):
        assert "employs" in vocab.active_config().predicates
        assert graph.entity("org:acme") is not None  # org/employs valid here
    with tenancy.use(authz.resolve_vault(alice, "home", data_root=root)):
        assert "employs" not in vocab.active_config().predicates
        assert graph.entity("org:acme") is None       # unknown in home's schema
        assert graph.entity("project:atlas") is not None


# ── DRILL 3: an ungranted vault is denied everywhere ────────────────────────


def test_drill_ungranted_vault_denied(env):
    root, alice, bob = env
    with pytest.raises(identity.Deny):
        authz.resolve_vault(alice, "locked", data_root=root)      # data layer
    with pytest.raises(identity.Deny):
        authz.open_authorized_vault(alice, "locked", data_root=root)  # surface choke point
    # bob (the owner) reaches it fine.
    assert authz.resolve_vault(bob, "locked", data_root=root).vault_id == "locked"


# ── DRILL 4: state (access + review) never leaks across vaults ──────────────


def test_drill_state_is_isolated(env):
    root, alice, bob = env
    with tenancy.use(authz.resolve_vault(alice, "work", data_root=root)):
        state.record_access("w1")
        state.enqueue_contradiction("w1", "ghost", "in work")
    with tenancy.use(authz.resolve_vault(alice, "home", data_root=root)):
        assert state.get_access("w1") is None
        assert state.list_open_reviews() == []


# ── Lifecycle: creation is permission- + quota-gated ────────────────────────


def test_creation_is_permission_gated(env):
    root, alice, bob = env
    readonly = identity.Principal(principal_id="ro", tenant_id="acme", role="readonly")
    with pytest.raises(authz.AuthorizationError):
        vaults.create_vault(readonly, "nope", data_root=root)  # readonly lacks vaults:create


def test_creation_respects_the_tenant_vault_quota(env, monkeypatch):
    root, alice, bob = env
    # work + home + locked = 3 non-default vaults already; cap at 3 → next create refused.
    monkeypatch.setattr(config, "MNESIS_TENANT_MAX_VAULTS", 3, raising=False)
    with pytest.raises(vaults.VaultManagementError) as ei:
        vaults.create_vault(alice, "extra", data_root=root)
    assert ei.value.reason == "vault_quota_exceeded"


# ── Lifecycle: management boundary (owner / tenant-admin) ───────────────────


def test_management_is_owner_or_admin_gated(env):
    root, alice, bob = env
    # bob (a member) may not rename/delete/grant on alice's vault.
    with pytest.raises(identity.Deny):
        vaults.rename_vault(bob, "work", "hacked", data_root=root)
    with pytest.raises(identity.Deny):
        vaults.delete_vault(bob, "work", confirm="work", data_root=root)
    # A tenant-admin may manage any vault in the tenant.
    admin = identity.Principal(principal_id="admin", tenant_id="acme", role="admin")
    assert vaults.rename_vault(admin, "work", "Work Vault", data_root=root).name == "Work Vault"
    # The owner may share it; the grantee then reaches it (re-authorized).
    vaults.grant_access(alice, "carol", "work", data_root=root)
    carol = identity.Principal(principal_id="carol", tenant_id="acme", role="member")
    assert authz.resolve_vault(carol, "work", data_root=root).vault_id == "work"
    vaults.revoke_access(alice, "carol", "work", data_root=root)
    with pytest.raises(identity.Deny):
        authz.resolve_vault(carol, "work", data_root=root)


# ── Lifecycle: deletion removes ALL data and is audited ─────────────────────


def test_deletion_removes_all_data_and_is_audited(env):
    root, alice, bob = env
    audit = vaults.VaultAuditLog(root / "vault_audit.jsonl")
    work_root = tenancy.context_for("acme", "work", data_root=root).root_path
    assert work_root.exists()

    res = vaults.delete_vault(alice, "work", confirm="work", data_root=root, audit=audit)
    assert res["removed_root"] is True
    assert not work_root.exists()  # store + caches + state + config + git all gone
    assert not tenancy.tenant_context_for("acme", data_root=root).vault_registry().exists("work")
    # The delete is refused thereafter (unknown) and denied without the confirm guard.
    with pytest.raises(vaults.VaultManagementError):
        vaults.delete_vault(alice, "home", confirm="WRONG", data_root=root)
    # The default vault is protected.
    with pytest.raises(vaults.VaultManagementError) as ei:
        vaults.delete_vault(alice, "default", confirm="default", data_root=root)
    assert ei.value.reason == "protected"

    actions = [(r["action"], r["vault_id"]) for r in audit.all()]
    assert ("delete", "work") in actions  # lifecycle op audited


# ── Quotas: per-vault limit binds within the tenant ─────────────────────────


def test_per_vault_quota_binds(env):
    root, alice, bob = env
    vaults.set_quota(alice, "home", max_pages=1, data_root=root)
    ctx = tenancy.context_for("acme", "home", data_root=root)
    # home already has 1 page → at limit; a further create is refused fail-closed.
    with tenancy.use(ctx):
        with pytest.raises(quotas.QuotaExceeded):
            quotas.require_capacity(ctx, adding_pages=1)
    # work is unaffected by home's quota.
    wctx = tenancy.context_for("acme", "work", data_root=root)
    with tenancy.use(wctx):
        quotas.require_capacity(wctx, adding_pages=1)  # no raise


# ── Migration: the default vault stays transparent + lossless ───────────────


def test_default_vault_migration_preserves_prior_behaviour(tmp_path, monkeypatch):
    root = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_ROOT", root, raising=False)
    monkeypatch.setattr(config, "MNESIS_LLM_STUB", True, raising=False)
    # A pre-vault per-tenant store.
    tdir = root / config.TENANTS_DIRNAME / "solo"
    (tdir / "pages").mkdir(parents=True)
    (tdir / "sources").mkdir(parents=True)
    (tdir / "pages" / "legacy.md").write_text(
        "---\nid: legacy\ntitle: A legacy page\n---\nAtlas uses Redis.\n", encoding="utf-8"
    )
    import subprocess
    subprocess.run(["git", "-C", str(tdir), "init", "-q"], check=True)

    ctx = tenancy.open_tenant("solo", data_root=root)  # transparent migration on first use
    assert ctx.vault_id == "default"
    with tenancy.use(ctx):
        assert any(p.id == "legacy" for p in store.list_pages())  # lossless
        search.rebuild()
        assert [h.id for h in search.search("legacy")] == ["legacy"]
        # Behaves exactly as the pre-vault (global-schema) pipeline.
        page = ingest.ingest_source("Redis caches. rel{project:atlas|uses|library:redis}", "s1")
        assert {"s": "project:atlas", "p": "uses", "o": "library:redis"} in page.relations
