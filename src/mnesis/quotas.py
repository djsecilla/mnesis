"""Per-tenant resource quotas (T7) — fairness and blast-radius containment.

A tenant has limits on how much it can store (page count, bytes). Limits come from
the tenant's own record (registry) if set, else the global config defaults
(``MNESIS_TENANT_MAX_*``); ``0`` means unlimited. Enforcement is **fail-closed** at
the ingest write boundary: a write that would exceed a limit is refused with a clear
:class:`QuotaExceeded`, surfaced to the caller (the MCP tool / CLI / API return the
reason rather than silently dropping).

Cross-tenant is already impossible (§16); quotas only bound a tenant *within* its
own root, so one tenant can never exhaust another's capacity.
"""

from __future__ import annotations

from . import config, tenancy
from .tenancy import TenantContext, VaultContext


class QuotaExceeded(Exception):
    """A write would exceed the tenant's/vault's resource quota (fail closed)."""


def _tighter(a: int, b: int) -> int:
    """The binding limit of two caps where ``0`` = unlimited: the smaller non-zero one
    (0 only when both are unlimited)."""
    caps = [c for c in (a, b) if c]
    return min(caps) if caps else 0


def limits_for(ctx: TenantContext) -> tuple[int, int]:
    """The **effective** ``(max_pages, max_bytes)`` for the bound context — the tenant
    limit (registry override else config default) **intersected with** the vault's own
    optional per-vault limit (V6). ``0`` = unlimited; a per-vault cap only ever *tightens*
    the tenant cap (a vault stays within its tenant's quota)."""
    tenant = tenancy.TenantRegistry().get(ctx.tenant_id)
    max_pages = (tenant.max_pages if tenant and tenant.max_pages else 0) or config.MNESIS_TENANT_MAX_PAGES
    max_bytes = (tenant.max_bytes if tenant and tenant.max_bytes else 0) or config.MNESIS_TENANT_MAX_BYTES
    if isinstance(ctx, VaultContext):
        vault = ctx.vault_registry().get(ctx.vault_id)
        if vault is not None:
            max_pages = _tighter(max_pages, vault.max_pages)
            max_bytes = _tighter(max_bytes, vault.max_bytes)
    return max_pages, max_bytes


def usage(ctx: TenantContext) -> tuple[int, int]:
    """Current ``(page_count, bytes_on_disk)`` for the tenant's canonical pages. The
    reserved OKF files (index.md/log.md) are not concepts and are excluded."""
    from .okf import RESERVED_FILES

    if not ctx.pages_dir.exists():
        return 0, 0
    files = [p for p in ctx.pages_dir.glob("*.md") if p.name not in RESERVED_FILES]
    total = sum(p.stat().st_size for p in files)
    return len(files), total


def require_capacity(ctx: TenantContext, *, adding_pages: int = 1) -> None:
    """Raise :class:`QuotaExceeded` if writing ``adding_pages`` more page(s) would
    exceed the tenant's page or storage quota. No bound principal needed — quotas
    are a per-tenant property. A no-op when both limits are unlimited (0)."""
    max_pages, max_bytes = limits_for(ctx)
    if not (max_pages or max_bytes):
        return
    where = f"tenant {ctx.tenant_id!r}"
    if isinstance(ctx, VaultContext):
        where += f" vault {ctx.vault_id!r}"
    pages, used = usage(ctx)
    if max_pages and pages + adding_pages > max_pages:
        raise QuotaExceeded(
            f"page quota exceeded for {where}: "
            f"{pages} page(s) at limit {max_pages} (cannot add {adding_pages})"
        )
    if max_bytes and used >= max_bytes:
        raise QuotaExceeded(
            f"storage quota exceeded for {where}: "
            f"{used} bytes at limit {max_bytes}"
        )
