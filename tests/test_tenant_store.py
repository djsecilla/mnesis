"""T1 — tenant model, TenantContext, per-tenant stores, migration (CLAUDE.md §16).

The store is multitenant BY CONSTRUCTION: it cannot be built or reached without a
TenantContext, two tenants' pages land under separate physical roots, a crafted
id/path can never escape a tenant root, and an existing single-store layout migrates
into ``tenants/default/`` non-destructively and idempotently.
"""

from __future__ import annotations

import subprocess

import pytest

from mnesis import config, search, state, store, tenancy


@pytest.fixture()
def data_root(tmp_path, monkeypatch):
    root = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_ROOT", root, raising=False)
    return root


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


# ── no global store: construction requires a TenantContext ──────────────────


def test_store_cannot_be_constructed_without_a_tenant_context():
    for bad in (None, "default", object(), {"tenant_id": "default"}):
        with pytest.raises(TypeError):
            store.Store(bad)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        search.SearchIndex(None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        state.StateStore("default")  # type: ignore[arg-type]


def test_module_store_is_unreachable_without_a_bound_tenant(data_root):
    # No active TenantContext bound at a boundary → fail closed (no ambient store).
    with pytest.raises(tenancy.NoTenantContextError):
        store.list_pages()
    with pytest.raises(tenancy.NoTenantContextError):
        store.write_page(store.Page(id="x", title="x"))
    with pytest.raises(tenancy.NoTenantContextError):
        search.search("anything")


# ── two tenants → separate physical roots ──────────────────────────────────


def test_two_tenants_write_pages_under_separate_roots(data_root):
    alpha = tenancy.create_tenant("alpha", data_root=data_root)
    beta = tenancy.create_tenant("beta", data_root=data_root)

    store.Store(alpha).write_page(store.Page(id="shared-id", title="Alpha secret", body="A"))
    store.Store(beta).write_page(store.Page(id="shared-id", title="Beta secret", body="B"))

    # Same id, fully isolated content under separate roots.
    assert store.Store(alpha).read_page("shared-id").title == "Alpha secret"
    assert store.Store(beta).read_page("shared-id").title == "Beta secret"
    assert alpha.root_path != beta.root_path
    assert (alpha.pages_dir / "shared-id.md").exists()
    assert (beta.pages_dir / "shared-id.md").exists()
    # Neither tenant's pages dir contains the other's file in any way.
    assert alpha.root_path not in beta.root_path.parents
    # Each tenant has its OWN git repo.
    assert (alpha.root_path / ".git").is_dir() and (beta.root_path / ".git").is_dir()
    assert _git(alpha.root_path, "rev-parse", "--show-toplevel").stdout.strip() == str(alpha.root_path)


def test_active_binding_routes_module_calls_to_the_right_tenant(data_root):
    alpha = tenancy.create_tenant("alpha", data_root=data_root)
    beta = tenancy.create_tenant("beta", data_root=data_root)
    with tenancy.use(alpha):
        store.write_page(store.Page(id="p", title="from alpha", body="a"))
    with tenancy.use(beta):
        store.write_page(store.Page(id="p", title="from beta", body="b"))
        assert [pg.id for pg in store.list_pages()] == ["p"]
        assert store.read_page("p").title == "from beta"
    with tenancy.use(alpha):
        assert store.read_page("p").title == "from alpha"


# ── path-traversal guard: an id/path can never escape the tenant root ───────


def test_crafted_ids_and_paths_cannot_escape_the_tenant_root(data_root):
    ctx = tenancy.create_tenant("alpha", data_root=data_root)
    for bad in ("../../etc/passwd", "..", ".", "", "a/b", "a\\b", "/etc/passwd", "../beta/pages/p"):
        with pytest.raises(tenancy.PathEscapeError):
            ctx.page_path(bad)
        with pytest.raises(tenancy.PathEscapeError):
            ctx.source_path(bad)
    # resolve() refuses an absolute escape and a traversal that climbs out.
    with pytest.raises(tenancy.PathEscapeError):
        ctx.resolve("pages", "..", "..", "secret")
    # A legitimate page id resolves inside the root.
    assert ctx.page_path("ok-page").is_relative_to(ctx.root_path)


def test_write_page_with_a_traversal_id_is_refused(data_root):
    ctx = tenancy.create_tenant("alpha", data_root=data_root)
    with tenancy.use(ctx):
        with pytest.raises(tenancy.PathEscapeError):
            store.write_page(store.Page(id="../escape", title="nope"))
        with pytest.raises(tenancy.PathEscapeError):
            store.write_source("../escape", "nope")


def test_invalid_tenant_ids_are_refused(data_root):
    for bad in ("../evil", "a/b", "UPPER", ".", "", "x y", "-leading"):
        with pytest.raises(tenancy.InvalidTenantId):
            tenancy.validate_tenant_id(bad)
        with pytest.raises(tenancy.InvalidTenantId):
            tenancy.context_for(bad, data_root=data_root)


# ── migration: legacy single-store layout → tenants/default/ ────────────────


def _make_legacy_layout(root):
    """An old single-store layout: pages/ + sources/ + .index/ + a top-level git repo."""
    (root / "pages").mkdir(parents=True)
    (root / "sources").mkdir(parents=True)
    (root / ".index").mkdir(parents=True)
    (root / "pages" / "legacy-page.md").write_text(
        "---\nid: legacy-page\ntitle: A legacy page\n---\nbody\n", encoding="utf-8"
    )
    (root / "sources" / "legacy-src.md").write_text("a redacted source\n", encoding="utf-8")
    (root / ".index" / "wiki.db").write_text("stale cache", encoding="utf-8")
    _git(root, "init", "-q")


def test_migration_moves_existing_data_into_default_losslessly(data_root):
    _make_legacy_layout(data_root)

    result = tenancy.migrate_legacy_to_default(data_root=data_root)
    assert result["migrated"] is True and result["tenant"] == "default"
    assert sorted(result["moved"]) == ["pages", "sources"]

    ctx = tenancy.context_for("default", data_root=data_root)
    # Canonical content moved, intact, under the default tenant root.
    assert (ctx.pages_dir / "legacy-page.md").exists()
    assert (ctx.sources_dir / "legacy-src.md").read_text(encoding="utf-8") == "a redacted source\n"
    # The default tenant is registered and has its own git repo with a commit.
    assert tenancy.TenantRegistry(data_root / config.REGISTRY_FILENAME).exists("default")
    assert (ctx.root_path / ".git").is_dir()
    assert _git(ctx.root_path, "rev-list", "--count", "HEAD").stdout.strip() != "0"
    # Non-destructive: the legacy top-level dirs were MOVED (no longer at the root).
    assert not (data_root / "pages").exists()

    # The migrated tenant is fully usable (search rebuild over the moved page).
    with tenancy.use(ctx):
        assert any(p.id == "legacy-page" for p in store.list_pages())
        search.rebuild()
        assert [h.id for h in search.search("legacy")] == ["legacy-page"]


def test_migration_is_idempotent_a_rerun_is_a_noop(data_root):
    _make_legacy_layout(data_root)
    first = tenancy.migrate_legacy_to_default(data_root=data_root)
    assert first["migrated"] is True

    ctx = tenancy.context_for("default", data_root=data_root)
    head_before = _git(ctx.root_path, "rev-parse", "HEAD").stdout.strip()

    second = tenancy.migrate_legacy_to_default(data_root=data_root)
    assert second["migrated"] is False and second["moved"] == []
    # No new commit, content unchanged.
    assert _git(ctx.root_path, "rev-parse", "HEAD").stdout.strip() == head_before
    assert (ctx.pages_dir / "legacy-page.md").exists()


def test_migration_on_a_fresh_install_just_provisions_default(data_root):
    # No legacy data present — migration provisions an empty default tenant, no move.
    result = tenancy.migrate_legacy_to_default(data_root=data_root)
    assert result["migrated"] is False and result["pages"] == 0
    ctx = tenancy.context_for("default", data_root=data_root)
    assert ctx.pages_dir.exists() and (ctx.root_path / ".git").is_dir()


def test_open_tenant_is_transparent_for_a_single_tenant_deployment(data_root):
    """A single-tenant deployment reaches its data as ``default`` with no manual
    migrate step: open_tenant migrates legacy data on first use."""
    _make_legacy_layout(data_root)
    ctx = tenancy.open_tenant("default", data_root=data_root)
    with tenancy.use(ctx):
        assert any(p.id == "legacy-page" for p in store.list_pages())
