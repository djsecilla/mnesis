"""Smoke test for the mnesis scaffold.

Asserts the package imports, config paths resolve sanely, and the wiki
directory tree is created on demand by config.ensure_dirs().
"""

from __future__ import annotations


def test_package_imports():
    import mnesis

    assert mnesis.__version__


def test_config_paths_resolve():
    from mnesis import config

    # Paths are absolute and nested correctly under MNESIS_ROOT.
    assert config.MNESIS_ROOT.is_absolute()
    assert config.PAGES_DIR == config.MNESIS_ROOT / "pages"
    assert config.SOURCES_DIR == config.MNESIS_ROOT / "sources"
    assert config.INDEX_DIR == config.MNESIS_ROOT / ".index"


def test_env_defaults():
    from mnesis import config

    assert config.MNESIS_LLM_MODEL  # non-empty string
    assert isinstance(config.MNESIS_FILEBACK_THRESHOLD, float)
    assert isinstance(config.MNESIS_LLM_STUB, bool)


def test_dirs_created_on_demand():
    from mnesis import config

    config.ensure_dirs()
    assert config.MNESIS_ROOT.is_dir()
    assert config.PAGES_DIR.is_dir()
    assert config.SOURCES_DIR.is_dir()
    assert config.INDEX_DIR.is_dir()
