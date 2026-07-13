"""V1 — Vault model, VaultContext, per-vault stores, migration (CLAUDE.md §16).

Vaults are a per-user, in-tenant isolation unit: the store is **vault-scoped BY
CONSTRUCTION**, mirroring the tenant primitive one level down. A store cannot be built
or reached without a :class:`VaultContext`; two vaults of the same tenant land under
separate physical roots and never share files; a crafted id/path can never escape a
vault root; and an existing per-tenant (or legacy single) store migrates into the
``default`` vault non-destructively and idempotently — tenant isolation unchanged.
"""

from __future__ import annotations

import subprocess

import pytest

from mnesis import config, search, state, store, tenancy


@pytest.fixture()
def data_root(tmp_path, monkeypatch):
    root = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_ROOT", root, raising=False)
    monkeypatch.setattr(config, "MNESIS_LLM_STUB", True, raising=False)
    return root


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


# ── no vault-less store: construction requires a VaultContext ────────────────


def test_store_cannot_be_constructed_without_a_vault_context(data_root):
    # Non-context values are refused.
    for bad in (None, "default", object(), {"tenant_id": "default"}):
        with pytest.raises(TypeError):
            store.Store(bad)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            search.SearchIndex(bad)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            state.StateStore(bad)  # type: ignore[arg-type]

    # A bare *tenant-level* handle is NOT a store handle — it, too, is refused.
    tctx = tenancy.tenant_context_for("alpha", data_root=data_root)
    assert not isinstance(tctx, tenancy.VaultContext)
    for factory in (store.Store, search.SearchIndex, state.StateStore):
        with pytest.raises(TypeError):
            factory(tctx)  # type: ignore[arg-type]

    # Only a VaultContext builds a store.
    vctx = tenancy.create_tenant("alpha", data_root=data_root)
    assert isinstance(vctx, tenancy.VaultContext)
    store.Store(vctx)  # no raise


def test_a_vault_belongs_to_exactly_one_tenant(data_root):
    vctx = tenancy.create_vault("alpha", "research", data_root=data_root)
    assert vctx.tenant_id == "alpha" and vctx.vault_id == "research"
    # The vault root is nested under its ONE tenant root.
    assert vctx.root_path.is_relative_to(vctx.tenant_root)
    assert vctx.tenant_root.name == "alpha"


# ── two vaults of one tenant → separate physical roots, never shared ─────────


def test_two_vaults_of_a_tenant_write_pages_under_separate_roots(data_root):
    work = tenancy.create_vault("alpha", "work", data_root=data_root)
    home = tenancy.create_vault("alpha", "home", data_root=data_root)

    store.Store(work).write_page(store.Page(id="shared-id", title="Work secret", body="W"))
    store.Store(home).write_page(store.Page(id="shared-id", title="Home secret", body="H"))

    # Same id, same tenant, fully isolated content under separate vault roots.
    assert store.Store(work).read_page("shared-id").title == "Work secret"
    assert store.Store(home).read_page("shared-id").title == "Home secret"
    assert work.root_path != home.root_path
    assert (work.pages_dir / "shared-id.md").exists()
    assert (home.pages_dir / "shared-id.md").exists()
    # Neither vault's root contains the other's; they are siblings under vaults/.
    assert work.root_path not in home.root_path.parents
    assert work.root_path.parent == home.root_path.parent == work.tenant_root / config.VAULTS_DIRNAME
    # Each vault has its OWN git repo.
    assert (work.root_path / ".git").is_dir() and (home.root_path / ".git").is_dir()
    assert _git(work.root_path, "rev-parse", "--show-toplevel").stdout.strip() == str(work.root_path)
    # The per-tenant vault registry records both, outside any vault root.
    reg_path = work.tenant_root / config.VAULT_REGISTRY_FILENAME
    assert reg_path.is_file() and not reg_path.is_relative_to(work.root_path)
    assert {v.vault_id for v in tenancy.list_vaults("alpha", data_root=data_root)} >= {"work", "home"}


def test_tenant_isolation_is_unchanged(data_root):
    # A vault under tenant alpha and a same-named vault under beta never share a root.
    a = tenancy.create_vault("alpha", "v", data_root=data_root)
    b = tenancy.create_vault("beta", "v", data_root=data_root)
    assert a.tenant_id != b.tenant_id
    assert a.root_path != b.root_path
    assert a.tenant_root not in b.root_path.parents


def test_active_binding_routes_module_calls_to_the_right_vault(data_root):
    work = tenancy.create_vault("alpha", "work", data_root=data_root)
    home = tenancy.create_vault("alpha", "home", data_root=data_root)
    with tenancy.use(work):
        store.write_page(store.Page(id="p", title="from work", body="w"))
    with tenancy.use(home):
        store.write_page(store.Page(id="p", title="from home", body="h"))
        assert [pg.id for pg in store.list_pages()] == ["p"]
        assert store.read_page("p").title == "from home"
    with tenancy.use(work):
        assert store.read_page("p").title == "from work"


# ── path-traversal guard: an id/path can never escape the VAULT root ─────────


def test_crafted_ids_and_paths_cannot_escape_the_vault_root(data_root):
    ctx = tenancy.create_vault("alpha", "work", data_root=data_root)
    for bad in ("../../etc/passwd", "..", ".", "", "a/b", "a\\b", "/etc/passwd", "../home/pages/p"):
        with pytest.raises(tenancy.PathEscapeError):
            ctx.page_path(bad)
        with pytest.raises(tenancy.PathEscapeError):
            ctx.source_path(bad)
    # resolve() refuses climbing out of the vault root (even into a sibling vault).
    with pytest.raises(tenancy.PathEscapeError):
        ctx.resolve("pages", "..", "..", "home", "pages", "p")
    with pytest.raises(tenancy.PathEscapeError):
        ctx.resolve("..", "..", "secret")
    # A legitimate page id resolves inside the vault root.
    assert ctx.page_path("ok-page").is_relative_to(ctx.root_path)


def test_invalid_vault_ids_are_refused(data_root):
    for bad in ("../evil", "a/b", "UPPER", ".", "", "x y", "-leading"):
        with pytest.raises(tenancy.InvalidVaultId):
            tenancy.validate_vault_id(bad)
        with pytest.raises(tenancy.InvalidVaultId):
            tenancy.context_for("alpha", bad, data_root=data_root)


# ── migration: existing per-tenant store → the default vault ─────────────────


def _make_pretenant_layout(root):
    """A pre-vault per-tenant store: tenants/alpha/{pages,sources} + a top-level git."""
    tdir = root / config.TENANTS_DIRNAME / "alpha"
    (tdir / "pages").mkdir(parents=True)
    (tdir / "sources").mkdir(parents=True)
    (tdir / "pages" / "legacy-page.md").write_text(
        "---\nid: legacy-page\ntitle: A legacy page\n---\nbody\n", encoding="utf-8"
    )
    (tdir / "sources" / "legacy-src.md").write_text("a redacted source\n", encoding="utf-8")
    _git(tdir, "init", "-q")


def test_tenant_store_migrates_into_default_vault_losslessly(data_root):
    _make_pretenant_layout(data_root)

    result = tenancy.migrate_tenant_to_default_vault("alpha", data_root=data_root)
    assert result["migrated"] is True and result["vault"] == "default"
    assert sorted(result["moved"]) == ["pages", "sources"]

    vctx = tenancy.context_for("alpha", "default", data_root=data_root)
    # Canonical content moved, intact, under the default vault root.
    assert (vctx.pages_dir / "legacy-page.md").exists()
    assert (vctx.sources_dir / "legacy-src.md").read_text(encoding="utf-8") == "a redacted source\n"
    # The default vault is registered and has its own git repo with a commit.
    assert tenancy.VaultRegistry(vctx.tenant_root / config.VAULT_REGISTRY_FILENAME).exists("default")
    assert (vctx.root_path / ".git").is_dir()
    assert _git(vctx.root_path, "rev-list", "--count", "HEAD").stdout.strip() != "0"
    # Non-destructive: the tenant-root dirs were MOVED (no longer at the tenant root).
    assert not (data_root / config.TENANTS_DIRNAME / "alpha" / "pages").exists()

    # The migrated vault is fully usable (search rebuild over the moved page).
    with tenancy.use(vctx):
        assert any(p.id == "legacy-page" for p in store.list_pages())
        search.rebuild()
        assert [h.id for h in search.search("legacy")] == ["legacy-page"]


def test_tenant_to_vault_migration_is_idempotent_a_rerun_is_a_noop(data_root):
    _make_pretenant_layout(data_root)
    first = tenancy.migrate_tenant_to_default_vault("alpha", data_root=data_root)
    assert first["migrated"] is True

    vctx = tenancy.context_for("alpha", "default", data_root=data_root)
    head_before = _git(vctx.root_path, "rev-parse", "HEAD").stdout.strip()

    second = tenancy.migrate_tenant_to_default_vault("alpha", data_root=data_root)
    assert second["migrated"] is False and second["moved"] == []
    assert _git(vctx.root_path, "rev-parse", "HEAD").stdout.strip() == head_before
    assert (vctx.pages_dir / "legacy-page.md").exists()


def test_legacy_single_store_converges_on_the_default_vault(data_root):
    # The pre-tenant single-store layout also lands in the default tenant's default vault.
    (data_root / "pages").mkdir(parents=True)
    (data_root / "sources").mkdir(parents=True)
    (data_root / "pages" / "p.md").write_text(
        "---\nid: p\ntitle: A page\n---\nbody\n", encoding="utf-8"
    )
    _git(data_root, "init", "-q")

    result = tenancy.migrate_legacy_to_default(data_root=data_root)
    assert result["migrated"] is True and result["vault"] == "default"

    vctx = tenancy.context_for("default", "default", data_root=data_root)
    assert (vctx.pages_dir / "p.md").exists()
    assert (vctx.root_path / ".git").is_dir()
    # Re-run is a no-op.
    again = tenancy.migrate_legacy_to_default(data_root=data_root)
    assert again["migrated"] is False and again["moved"] == []


# ── the default vault is transparent (existing single-tenant flow) ───────────


def test_default_vault_is_transparent(data_root):
    """open_tenant('default') yields a usable default-vault store with no vault arg —
    the on-disk layout is tenants/default/vaults/default/{pages,sources,.cache}."""
    ctx = tenancy.open_tenant("default", data_root=data_root)
    assert isinstance(ctx, tenancy.VaultContext)
    assert ctx.tenant_id == "default" and ctx.vault_id == "default"
    expected = data_root / config.TENANTS_DIRNAME / "default" / config.VAULTS_DIRNAME / "default"
    assert ctx.root_path == expected.resolve()
    assert ctx.pages_dir == expected.resolve() / "pages"
    assert ctx.cache_dir == expected.resolve() / ".cache"
    with tenancy.use(ctx):
        store.write_page(store.Page(id="hello", title="Hello world", body="hi"))
        search.rebuild()
        assert [h.id for h in search.search("hello")] == ["hello"]
