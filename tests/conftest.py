"""Shared test fixtures for the tenant-scoped core.

The store is multitenant by construction (CLAUDE.md §16): there is no global store,
and it can only be reached with a :class:`~mnesis.tenancy.TenantContext` bound at a
boundary. These fixtures stand in for that boundary — they point ``DATA_ROOT`` at a
per-test temp dir, provision the ``default`` tenant (its own git repo), and bind it
for the duration of the test, so existing single-tenant tests run unchanged against
``default``.
"""

from __future__ import annotations

import pytest

from mnesis import config, tenancy


def bind_tenant(tmp_path, monkeypatch, tenant_id: str | None = None):
    """Point DATA_ROOT at ``tmp_path/data``, provision + bind ``tenant_id`` (default),
    and return its :class:`TenantContext`. Caller-managed binding lives for the test
    via monkeypatch teardown plus an explicit unbind in the fixtures below."""
    tid = tenant_id or config.DEFAULT_TENANT_ID
    data_root = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_ROOT", data_root, raising=False)
    # The core suite is offline: force the deterministic LLM stub for any test that
    # binds a tenant (test_llm_provider, which tests provider switching, does not).
    monkeypatch.setattr(config, "MNESIS_LLM_STUB", True, raising=False)
    ctx = tenancy.create_tenant(tid, data_root=data_root)
    token = tenancy.bind(ctx)
    return ctx, token


@pytest.fixture()
def tenant(tmp_path, monkeypatch):
    """A bound ``default`` tenant rooted under a fresh temp data root (the common
    case). Yields its :class:`TenantContext`; the store is reachable only while bound."""
    ctx, token = bind_tenant(tmp_path, monkeypatch)
    try:
        yield ctx
    finally:
        tenancy.unbind(token)
