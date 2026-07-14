"""Vault lifecycle & administration (V6) — CLAUDE.md §16 Vaults.

The management layer over the per-vault stores (V1) + access control (V2): a principal
creates, lists, renames, deletes, and shares vaults **within its own tenant**, under a
clear management boundary and with every mutating op audited.

**The management boundary.**
  - **Creating** a vault requires the ``vaults:create`` permission (admin + member — a
    member may create their own vaults and becomes the owner). Creation respects the
    tenant's **vault quota** (:data:`MNESIS_TENANT_MAX_VAULTS`).
  - **Managing an existing vault** (rename / delete / set-quota / grant / revoke) is
    limited to the **vault owner** OR a **tenant-admin** — never another member, never a
    cross-tenant principal.
  - The transparent ``default`` vault cannot be deleted (it is the tenant's baseline).

**Deletion removes ALL of a vault's data** — its store (pages/sources), caches
(wiki.db/graph.db/state.db), its per-vault schema config, git history, and every grant to
it — behind a guarded confirm (must equal the vault id). Every lifecycle op is recorded in
the append-only :class:`VaultAuditLog` (``DATA_ROOT/vault_audit.jsonl``, outside every
vault root). Vault access is always re-authorized at the surfaces (V5, `authz.resolve_vault`);
this module governs *who may change the set of vaults and grants*.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from . import authz, config, tenancy
from .identity import Deny, Principal


class VaultManagementError(Exception):
    """A vault-management op was refused. Carries a machine ``reason``."""

    def __init__(self, message: str, *, reason: str = "denied") -> None:
        super().__init__(message)
        self.reason = reason


# --- audit -----------------------------------------------------------------


class VaultAuditLog:
    """Append-only JSONL of vault-lifecycle events — never inside a vault root."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else config.vault_audit_path()

    def record(self, action: str, *, tenant_id: str, vault_id: str, actor: str | None, **detail) -> dict:
        rec = {
            "ts": config.now_iso(), "action": action, "tenant_id": tenant_id,
            "vault_id": vault_id, "actor": actor, **detail,
        }
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


# --- the management boundary -----------------------------------------------


def _is_tenant_admin(principal: Principal) -> bool:
    return "admin" in getattr(principal, "roles", frozenset()) or principal.role == "admin"


def _require_same_tenant(principal: Principal, tenant_id: str) -> None:
    if principal is None or principal.tenant_id != tenant_id:
        raise Deny("cross-tenant vault management", reason="cross_tenant")


def _require_manager(principal: Principal, tenant_id: str, vault: "tenancy.Vault") -> None:
    """Owner-or-tenant-admin gate for managing an existing vault (fail closed)."""
    _require_same_tenant(principal, tenant_id)
    if _is_tenant_admin(principal):
        return
    if vault.owner_principal is not None and vault.owner_principal == principal.principal_id:
        return
    raise Deny(
        f"{principal.principal_id} may not manage vault {vault.vault_id!r} "
        "(owner or tenant-admin only)",
        reason="not_vault_manager",
    )


def _registry(tenant_id: str, data_root=None) -> "tenancy.VaultRegistry":
    return tenancy.tenant_context_for(tenant_id, data_root=data_root).vault_registry()


# --- lifecycle -------------------------------------------------------------


def create_vault(
    principal: Principal,
    vault_id: str,
    *,
    name: str | None = None,
    schema=None,
    max_pages: int = 0,
    max_bytes: int = 0,
    data_root=None,
    audit: VaultAuditLog | None = None,
) -> "tenancy.VaultContext":
    """Create a vault owned by ``principal`` in its own tenant. Permission-gated
    (``vaults:create``) and bounded by the tenant's **vault quota**; provisions the store
    + git + caches + a config (the given ``schema`` else the default). Audited."""
    tenant_id = principal.tenant_id
    authz.require(principal, authz.VAULTS_CREATE, context={"tenant_id": tenant_id})
    tenancy.validate_vault_id(vault_id)
    reg = _registry(tenant_id, data_root)
    if reg.exists(vault_id):
        raise VaultManagementError(f"vault {vault_id!r} already exists", reason="exists")
    _require_tenant_vault_capacity(tenant_id, reg, data_root=data_root)

    ctx = tenancy.create_vault(
        tenant_id, vault_id, name=name, owner_principal=principal.principal_id, data_root=data_root
    )
    if max_pages or max_bytes:
        reg.set_quota(vault_id, max_pages=max_pages, max_bytes=max_bytes)
    if schema is not None:
        from . import vocab
        vocab.save_config(ctx, schema)
    (audit or VaultAuditLog()).record(
        "create", tenant_id=tenant_id, vault_id=vault_id, actor=principal.principal_id,
        owner=principal.principal_id, root=str(ctx.root_path),
    )
    return ctx


def list_vaults(principal: Principal, *, data_root=None) -> list["tenancy.Vault"]:
    """The vaults ``principal`` may reach in its tenant (owned ∪ granted ∪ the transparent
    ``default``), as :class:`Vault` records — the data behind a 'my vaults' listing."""
    reg = _registry(principal.tenant_id, data_root)
    accessible = authz.accessible_vaults(principal, data_root=data_root)
    out = [v for v in reg.list() if v.vault_id in accessible]
    # The transparent default vault is always reachable even if not a registry record yet.
    if config.DEFAULT_VAULT_ID in accessible and not any(v.vault_id == config.DEFAULT_VAULT_ID for v in out):
        out.append(tenancy.Vault(vault_id=config.DEFAULT_VAULT_ID, tenant_id=principal.tenant_id, name="default"))
    return sorted(out, key=lambda v: v.vault_id)


def rename_vault(
    principal: Principal, vault_id: str, name: str, *, data_root=None,
    audit: VaultAuditLog | None = None,
) -> "tenancy.Vault":
    """Rename a vault's display ``name`` (the id/path never changes). Owner/admin only."""
    reg = _registry(principal.tenant_id, data_root)
    vault = _get_or_deny(reg, vault_id)
    _require_manager(principal, principal.tenant_id, vault)
    updated = reg.rename(vault_id, name)
    (audit or VaultAuditLog()).record(
        "rename", tenant_id=principal.tenant_id, vault_id=vault_id,
        actor=principal.principal_id, name=name,
    )
    return updated


def set_quota(
    principal: Principal, vault_id: str, *, max_pages: int | None = None,
    max_bytes: int | None = None, data_root=None, audit: VaultAuditLog | None = None,
) -> "tenancy.Vault":
    """Set a vault's per-vault quota (within the tenant's). Owner/admin only, audited."""
    reg = _registry(principal.tenant_id, data_root)
    vault = _get_or_deny(reg, vault_id)
    _require_manager(principal, principal.tenant_id, vault)
    updated = reg.set_quota(vault_id, max_pages=max_pages, max_bytes=max_bytes)
    (audit or VaultAuditLog()).record(
        "set_quota", tenant_id=principal.tenant_id, vault_id=vault_id,
        actor=principal.principal_id, max_pages=updated.max_pages, max_bytes=updated.max_bytes,
    )
    return updated


def delete_vault(
    principal: Principal, vault_id: str, *, confirm: str | bool, data_root=None,
    audit: VaultAuditLog | None = None,
) -> dict:
    """Delete a vault: remove its ENTIRE root (store/sources/caches/state/config/git), its
    registry record, and every grant to it. **Guarded** — ``confirm`` must equal the
    vault id (or ``True``). Owner/admin only; the ``default`` vault is protected. Audited."""
    if vault_id == config.DEFAULT_VAULT_ID:
        raise VaultManagementError("the default vault cannot be deleted", reason="protected")
    reg = _registry(principal.tenant_id, data_root)
    vault = _get_or_deny(reg, vault_id)
    _require_manager(principal, principal.tenant_id, vault)
    if confirm is not True and confirm != vault_id:
        raise VaultManagementError(
            f"delete refused: confirm must equal the vault id {vault_id!r} (guard against loss)",
            reason="confirm_mismatch",
        )
    ctx = tenancy.context_for(principal.tenant_id, vault_id, data_root=data_root)
    removed_root = False
    if ctx.root_path.exists():
        shutil.rmtree(ctx.root_path)
        removed_root = True
    reg.remove(vault_id)  # registry record + dangling grants
    (audit or VaultAuditLog()).record(
        "delete", tenant_id=principal.tenant_id, vault_id=vault_id,
        actor=principal.principal_id, removed_root=removed_root,
    )
    return {"tenant_id": principal.tenant_id, "vault_id": vault_id, "removed_root": removed_root}


# --- grants (owner/admin manage; the runtime check is authz.resolve_vault) --


def grant_access(
    principal: Principal, target_principal: str, vault_id: str, *, role: str | None = None,
    data_root=None, audit: VaultAuditLog | None = None,
) -> None:
    """Grant ``target_principal`` access to ``vault_id`` (optionally a per-vault role).
    Owner/admin only, audited."""
    reg = _registry(principal.tenant_id, data_root)
    vault = _get_or_deny(reg, vault_id)
    _require_manager(principal, principal.tenant_id, vault)
    reg.grant(target_principal, vault_id, role)
    (audit or VaultAuditLog()).record(
        "grant", tenant_id=principal.tenant_id, vault_id=vault_id, actor=principal.principal_id,
        target=target_principal, role=role,
    )


def revoke_access(
    principal: Principal, target_principal: str, vault_id: str, *, data_root=None,
    audit: VaultAuditLog | None = None,
) -> bool:
    """Revoke ``target_principal``'s grant to ``vault_id`` (ownership is unaffected).
    Owner/admin only, audited."""
    reg = _registry(principal.tenant_id, data_root)
    vault = _get_or_deny(reg, vault_id)
    _require_manager(principal, principal.tenant_id, vault)
    removed = reg.revoke_grant(target_principal, vault_id)
    (audit or VaultAuditLog()).record(
        "revoke", tenant_id=principal.tenant_id, vault_id=vault_id, actor=principal.principal_id,
        target=target_principal, removed=removed,
    )
    return removed


# --- helpers ---------------------------------------------------------------


def _get_or_deny(reg: "tenancy.VaultRegistry", vault_id: str) -> "tenancy.Vault":
    v = reg.get(vault_id)
    if v is None:
        raise VaultManagementError(f"unknown vault {vault_id!r}", reason="unknown_vault")
    return v


def _require_tenant_vault_capacity(tenant_id: str, reg: "tenancy.VaultRegistry", *, data_root=None) -> None:
    """Fail closed if creating another vault would exceed the tenant's vault quota
    (``MNESIS_TENANT_MAX_VAULTS``; 0 = unlimited)."""
    limit = config.MNESIS_TENANT_MAX_VAULTS
    if not limit:
        return
    # The default vault does not count against the quota (it is the baseline).
    existing = [v for v in reg.list() if v.vault_id != config.DEFAULT_VAULT_ID]
    if len(existing) >= limit:
        raise VaultManagementError(
            f"vault quota exceeded for tenant {tenant_id!r}: {len(existing)} at limit {limit}",
            reason="vault_quota_exceeded",
        )
