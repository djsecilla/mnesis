"""System administration & tenant lifecycle (T7).

Provisioning, suspend/resume, and deletion of tenants live here, behind a
**system-admin boundary**: only a :class:`~mnesis.auth.Principal` resolved from a
*system-admin* credential (``auth.resolve_admin``) may manage tenants — a tenant
principal can never perform a lifecycle op or see another tenant. Every lifecycle
op is recorded in a **system audit log** (``DATA_ROOT/system_audit.jsonl``), which
is separate from any tenant's git-history audit and lives OUTSIDE every tenant root.

Lifecycle:
  - **provision** — create the tenant root + its own git repo + cache dirs, then
    issue its initial tenant-admin credential (returned once).
  - **list** — the tenants the system knows.
  - **suspend / resume** — deny / restore access while **retaining** all data.
  - **delete** — remove the tenant's root, caches, credentials, registry record, and
    (best-effort) its agent state — behind a guarded confirmation.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from . import auth, config, tenancy
from .auth import CredentialStore, Principal, is_system_admin
from .config import now_iso as _now_iso  # local alias keeps call sites unchanged


class AdminAccessError(Exception):
    """A non-system-admin attempted a tenant-lifecycle operation (fail closed)."""


def require_admin(principal: Principal | None) -> Principal:
    """Authorize a lifecycle op: the principal must be the system admin, else
    :class:`AdminAccessError`. Tenant principals (any role) are refused."""
    if not is_system_admin(principal):
        who = getattr(principal, "principal_id", "?")
        raise AdminAccessError(f"{who!r} is not the system admin; tenant lifecycle is admin-only")
    return principal  # type: ignore[return-value]


# --- System audit log (OUTSIDE any tenant root) ----------------------------


class SystemAuditLog:
    """Append-only JSONL of system/lifecycle events — never inside a tenant root."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else config.system_audit_path()

    def record(self, action: str, *, tenant_id: str | None, actor: str | None, **detail) -> dict:
        rec = {"ts": _now_iso(), "action": action, "tenant_id": tenant_id, "actor": actor, **detail}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    def all(self) -> list[dict]:
        if not self.path.is_file():
            return []
        out = []
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out


def _registry(data_root: Path | str | None) -> tenancy.TenantRegistry:
    if data_root is None:
        return tenancy.TenantRegistry()
    return tenancy.TenantRegistry(Path(data_root) / config.REGISTRY_FILENAME)


# --- Bootstrap the root of trust (local operator) --------------------------


def bootstrap_admin(
    principal_id: str = "root",
    *,
    cred_store: CredentialStore | None = None,
    audit: SystemAuditLog | None = None,
) -> tuple[str, "auth.Credential"]:
    """Mint the first system-admin credential (the lifecycle root of trust). A local
    operator action — like generating the first key on the box. Returns the raw token
    once. Audited."""
    store = cred_store or CredentialStore()
    raw, cred = store.issue_system_admin(principal_id)
    (audit or SystemAuditLog()).record("bootstrap_admin", tenant_id=None, actor=principal_id,
                                       credential_id=cred.id)
    return raw, cred


# --- Lifecycle (admin-only) -------------------------------------------------


def provision_tenant(
    tenant_id: str,
    name: str | None = None,
    *,
    admin: Principal,
    admin_principal: str = "admin",
    registry: tenancy.TenantRegistry | None = None,
    cred_store: CredentialStore | None = None,
    audit: SystemAuditLog | None = None,
    data_root: Path | str | None = None,
) -> dict:
    """Provision a tenant: create its root + git repo + cache dirs and issue its
    initial **tenant-admin** credential. Returns
    ``{tenant_id, credential_id, token}`` — ``token`` shown ONCE. Admin-only, audited."""
    require_admin(admin)
    reg = registry or _registry(data_root)
    ctx = tenancy.create_tenant(tenant_id, name, registry=reg, data_root=data_root)
    store = cred_store or CredentialStore(
        (Path(data_root) / config.CREDENTIALS_FILENAME) if data_root else None
    )
    raw, cred = store.issue(tenant_id, admin_principal, "admin", name=f"{tenant_id}-initial-admin")
    (audit or SystemAuditLog()).record(
        "provision", tenant_id=tenant_id, actor=admin.principal_id,
        credential_id=cred.id, root=str(ctx.root_path),
    )
    return {"tenant_id": tenant_id, "credential_id": cred.id, "token": raw, "root": str(ctx.root_path)}


def list_tenants(
    *, admin: Principal, registry: tenancy.TenantRegistry | None = None,
    data_root: Path | str | None = None,
) -> list[tenancy.Tenant]:
    """List the tenants the system knows. Admin-only."""
    require_admin(admin)
    return (registry or _registry(data_root)).list()


def suspend_tenant(
    tenant_id: str, *, admin: Principal,
    registry: tenancy.TenantRegistry | None = None, audit: SystemAuditLog | None = None,
    data_root: Path | str | None = None,
) -> tenancy.Tenant:
    """Suspend a tenant — deny access while RETAINING its data. Admin-only, audited."""
    require_admin(admin)
    tenant = (registry or _registry(data_root)).set_status(tenant_id, "suspended")
    (audit or SystemAuditLog()).record("suspend", tenant_id=tenant_id, actor=admin.principal_id)
    return tenant


def resume_tenant(
    tenant_id: str, *, admin: Principal,
    registry: tenancy.TenantRegistry | None = None, audit: SystemAuditLog | None = None,
    data_root: Path | str | None = None,
) -> tenancy.Tenant:
    """Resume a suspended tenant. Admin-only, audited."""
    require_admin(admin)
    tenant = (registry or _registry(data_root)).set_status(tenant_id, "active")
    (audit or SystemAuditLog()).record("resume", tenant_id=tenant_id, actor=admin.principal_id)
    return tenant


def set_quota(
    tenant_id: str, *, admin: Principal, max_pages: int | None = None, max_bytes: int | None = None,
    registry: tenancy.TenantRegistry | None = None, audit: SystemAuditLog | None = None,
    data_root: Path | str | None = None,
) -> tenancy.Tenant:
    """Set a tenant's per-tenant resource quotas. Admin-only, audited."""
    require_admin(admin)
    tenant = (registry or _registry(data_root)).set_quota(tenant_id, max_pages=max_pages, max_bytes=max_bytes)
    (audit or SystemAuditLog()).record(
        "set_quota", tenant_id=tenant_id, actor=admin.principal_id,
        max_pages=tenant.max_pages, max_bytes=tenant.max_bytes,
    )
    return tenant


def delete_tenant(
    tenant_id: str,
    *,
    admin: Principal,
    confirm: str | bool,
    registry: tenancy.TenantRegistry | None = None,
    cred_store: CredentialStore | None = None,
    audit: SystemAuditLog | None = None,
    data_root: Path | str | None = None,
    agent_state_base: Path | str | None = None,
) -> dict:
    """Delete a tenant: remove its root (pages/sources/.cache/.git), its credentials,
    its registry record, and (best-effort) its agent state. **Guarded**: ``confirm``
    must equal the ``tenant_id`` (or ``True``). Admin-only, audited — the audit record
    survives (it is outside the tenant root)."""
    require_admin(admin)
    if confirm is not True and confirm != tenant_id:
        raise AdminAccessError(
            f"delete refused: confirm must equal the tenant id {tenant_id!r} (guard against accidental loss)"
        )
    reg = registry or _registry(data_root)
    ctx = tenancy.context_for(tenant_id, data_root=data_root)
    removed_root = False
    if ctx.root_path.exists():
        shutil.rmtree(ctx.root_path)
        removed_root = True
    store = cred_store or CredentialStore(
        (Path(data_root) / config.CREDENTIALS_FILENAME) if data_root else None
    )
    creds_removed = store.remove_tenant(tenant_id)
    reg.remove(tenant_id)

    # Best-effort: the agent layer's per-tenant governance state (separate package /
    # deploy). Resolve its base from the arg or the env it publishes; never imported.
    agent_removed = False
    base = agent_state_base or os.environ.get("MNESIS_AGENTS_STATE_BASE")
    if base:
        agent_root = Path(base).expanduser() / "tenants" / tenant_id
        if agent_root.exists():
            shutil.rmtree(agent_root, ignore_errors=True)
            agent_removed = True

    (audit or SystemAuditLog()).record(
        "delete", tenant_id=tenant_id, actor=admin.principal_id,
        removed_root=removed_root, credentials_removed=creds_removed, agent_state_removed=agent_removed,
    )
    return {
        "tenant_id": tenant_id, "removed_root": removed_root,
        "credentials_removed": creds_removed, "agent_state_removed": agent_removed,
    }
