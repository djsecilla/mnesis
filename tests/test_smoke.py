"""Smoke test for the mnesis scaffold.

Asserts the package imports, the multitenant data root resolves sanely, and a
tenant's canonical store + cache directories are created on demand (CLAUDE.md §16).
There is no global store: paths are resolved per-tenant from a TenantContext.
"""

from __future__ import annotations


def test_package_imports():
    import mnesis

    assert mnesis.__version__


def test_data_root_resolves_and_has_no_global_store():
    from mnesis import config

    # The data root is absolute; tenants + the registry live beside each other.
    assert config.DATA_ROOT.is_absolute()
    assert config.tenants_dir() == config.DATA_ROOT / config.TENANTS_DIRNAME
    assert config.registry_path() == config.DATA_ROOT / config.REGISTRY_FILENAME
    # The old global store paths are gone (no ambient store).
    for removed in ("PAGES_DIR", "SOURCES_DIR", "INDEX_DIR"):
        assert not hasattr(config, removed)


def test_tenant_paths_nest_under_the_tenant_root(tmp_path):
    from mnesis import tenancy

    ctx = tenancy.context_for("default", data_root=tmp_path)
    assert ctx.pages_dir == ctx.root_path / "pages"
    assert ctx.sources_dir == ctx.root_path / "sources"
    assert ctx.cache_dir == ctx.root_path / ".cache"
    assert ctx.root_path.is_relative_to(tmp_path)


def test_env_defaults():
    from mnesis import config

    assert config.MNESIS_LLM_MODEL  # non-empty string
    assert isinstance(config.MNESIS_FILEBACK_THRESHOLD, float)
    assert isinstance(config.MNESIS_LLM_STUB, bool)


def test_tenant_dirs_created_on_demand(tmp_path):
    from mnesis import tenancy

    ctx = tenancy.create_tenant("default", data_root=tmp_path)
    assert ctx.root_path.is_dir()
    assert ctx.pages_dir.is_dir()
    assert ctx.sources_dir.is_dir()
    assert ctx.cache_dir.is_dir()
    assert (ctx.root_path / ".git").is_dir()  # its own repo
