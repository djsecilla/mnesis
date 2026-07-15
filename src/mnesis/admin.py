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

from . import audit as _authaudit
from . import auth, authz, config, identity, providers, tenancy, tokens
from .auth import CredentialStore, Principal, is_system_admin
from .config import now_iso as _now_iso  # local alias keeps call sites unchanged


class AdminAccessError(Exception):
    """A non-system-admin attempted a tenant-lifecycle operation (fail closed)."""


class AlreadyBootstrapped(Exception):
    """A system admin already exists; bootstrap refuses to clobber it (guarded)."""


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
    """Mint the first system-admin **token** credential (the lifecycle root of trust). A
    local operator action — like generating the first key on the box. Returns the raw
    token once. Audited. (IAM2 adds the password variant :func:`bootstrap_system_admin`.)"""
    store = cred_store or CredentialStore()
    raw, cred = store.issue_system_admin(principal_id)
    (audit or SystemAuditLog()).record("bootstrap_admin", tenant_id=None, actor=principal_id,
                                       credential_id=cred.id)
    return raw, cred


def bootstrap_system_admin(
    principal_id: str,
    password: str,
    *,
    cred_store: CredentialStore | None = None,
    audit: SystemAuditLog | None = None,
) -> "auth.Credential":
    """Securely bootstrap the first system-admin from an **operator-supplied password**
    (IAM2). There is **no default/hardcoded credential** — the operator must supply the
    password (CLI ``--password``, ``MNESIS_BOOTSTRAP_PASSWORD``, or a prompt).

    **Guarded + idempotent:** if a system admin already exists this raises
    :class:`AlreadyBootstrapped` and changes nothing — it can never silently reset an
    established root of trust. The password is policy-checked and stored argon2id;
    the plaintext is never logged. Audited."""
    from . import providers  # local import: providers depends on identity, avoid cycles

    store = cred_store or CredentialStore()
    if store.has_system_admin():
        raise AlreadyBootstrapped(
            "a system admin already exists; refusing to clobber it (revoke it explicitly first)"
        )
    providers.check_password_policy(password)  # fail before any write on a weak password
    cred = store.issue_system_admin_password(principal_id, password, name="bootstrap-system-admin")
    (audit or SystemAuditLog()).record(
        "bootstrap_system_admin", tenant_id=None, actor=principal_id, credential_id=cred.id
    )
    return cred


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
        credential_id=cred.id, root=str(ctx.tenant_root),
    )
    return {"tenant_id": tenant_id, "credential_id": cred.id, "token": raw, "root": str(ctx.tenant_root)}


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
    # Remove the whole TENANT root (all its vaults + caches + git + vault registry),
    # not just one vault.
    tenant_root = tenancy.tenant_context_for(tenant_id, data_root=data_root).root_path
    removed_root = False
    if tenant_root.exists():
        shutil.rmtree(tenant_root)
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


# --- User lifecycle (tenant-admin scoped, IAM8) -----------------------------
# A tenant-admin manages users WITHIN their own tenant. The boundary is the PDP:
# users:manage + tenant match — so a tenant-admin of A can never touch tenant B
# (cross_tenant deny), and a member/readonly/agent can never manage users at all.


class UserManagementError(Exception):
    """A caller attempted user management it is not authorized for (fail closed)."""


def _require_tenant_admin(actor: Principal | None, tenant_id: str) -> None:
    if not authz.authorize(actor, authz.USERS_MANAGE, context={"tenant_id": tenant_id}):
        who = getattr(actor, "principal_id", "?")
        raise UserManagementError(
            f"{who!r} may not manage users in tenant {tenant_id!r} "
            "(requires the admin role within that tenant)"
        )


def _require_role_assign(actor: Principal | None, tenant_id: str) -> None:
    """Only an ``admin`` may assign/change a role (R1). Flows through the SAME PDP —
    the ``roles:assign`` permission (admin-only) + tenant match; no parallel authz."""
    if not authz.authorize(actor, authz.ROLES_ASSIGN, context={"tenant_id": tenant_id}):
        who = getattr(actor, "principal_id", "?")
        raise UserManagementError(
            f"{who!r} may not assign roles in tenant {tenant_id!r} (admin only)"
        )


def _active_admin_ids(store: CredentialStore, tenant_id: str) -> set[str]:
    """The principal ids in ``tenant_id`` that are **active admins** (canonical role
    ``admin`` on at least one active credential)."""
    out: set[str] = set()
    for u in store.principals_for_tenant(tenant_id):
        if u["active"] and any(identity.canonical_role(r) == identity.ADMIN_ROLE for r in u["roles"]):
            out.add(u["principal_id"])
    return out


def _refuse_last_admin_lockout(store: CredentialStore, tenant_id: str, principal_id: str, op: str) -> None:
    """Refuse an op that would remove the **last active admin** of a tenant (no lockout):
    demote / deactivate / delete. A no-op if the target is not the sole active admin."""
    admins = _active_admin_ids(store, tenant_id)
    if admins == {principal_id}:
        raise UserManagementError(
            f"refusing to {op} {principal_id!r}: it is the last active admin of tenant "
            f"{tenant_id!r} (no-lockout rule)"
        )


def provision_user(
    tenant_id: str,
    principal_id: str,
    role: str,
    password: str,
    *,
    actor: Principal,
    cred_store: CredentialStore | None = None,
    provider: "providers.LocalPasswordProvider | None" = None,
) -> "auth.Credential":
    """Provision a user in ``tenant_id`` with ``role`` and a password (argon2id). The
    ``actor`` must be a tenant-admin of that tenant. Audited (no secret)."""
    _require_tenant_admin(actor, tenant_id)
    role = identity.canonical_role(role)  # store the canonical two-role vocabulary (member→user)
    prov = provider or providers.LocalPasswordProvider(store=cred_store or CredentialStore())
    rec = prov.register(tenant_id, principal_id, role, password)
    _authaudit.record(
        "user_provisioned", tenant_id=tenant_id, principal_id=principal_id,
        credential_id=rec.id, action="users:manage", result="ok",
        actor=actor.principal_id, role=role,
    )
    return rec


def deactivate_user(
    tenant_id: str,
    principal_id: str,
    *,
    actor: Principal,
    cred_store: CredentialStore | None = None,
    token_service: "tokens.TokenService | None" = None,
) -> dict:
    """Deactivate a user: **force-revoke** every credential AND runtime token they hold
    (immediate everywhere). Tenant-admin only, audited. The **last active admin** cannot
    be deactivated (no-lockout, R1)."""
    _require_tenant_admin(actor, tenant_id)
    store = cred_store or CredentialStore()
    _refuse_last_admin_lockout(store, tenant_id, principal_id, "deactivate")
    n_creds = store.revoke_for_principal(tenant_id, principal_id)
    n_tokens = (token_service or tokens.TokenService()).revoke_all_for_principal(tenant_id, principal_id)
    _authaudit.record(
        "user_deactivated", tenant_id=tenant_id, principal_id=principal_id,
        action="users:manage", result="ok", actor=actor.principal_id,
        credentials_revoked=n_creds, tokens_revoked=n_tokens,
    )
    return {"credentials_revoked": n_creds, "tokens_revoked": n_tokens}


def set_user_role(
    tenant_id: str,
    principal_id: str,
    role: str,
    *,
    actor: Principal,
    cred_store: CredentialStore | None = None,
) -> int:
    """Assign ``role`` to a user (updates all their credentials), normalised to the
    canonical two-role vocabulary (``member`` → ``user``). **Admin only** (`roles:assign`
    via the PDP). Enforces the R1 rules, fail-closed: a principal can **never** change its
    own role (no self-escalation), and the **last active admin** cannot be demoted
    (no-lockout). Audited. Returns the number of credentials updated."""
    _require_role_assign(actor, tenant_id)
    new_role = identity.canonical_role(role)
    identity.validate_role(new_role)  # reject unknown roles (e.g. "superuser")

    # No self-role-change: a principal can never escalate (or alter) its own role.
    if actor is not None and actor.principal_id == principal_id:
        raise UserManagementError(
            f"{actor.principal_id!r} may not change its own role (no self-escalation)"
        )

    store = cred_store or CredentialStore()
    # No-lockout: refuse demoting the last active admin away from `admin`.
    if new_role != identity.ADMIN_ROLE and principal_id in _active_admin_ids(store, tenant_id):
        _refuse_last_admin_lockout(store, tenant_id, principal_id, "demote")

    updated = 0
    for rec in store.list_for_tenant(tenant_id):
        if rec.principal_id == principal_id:
            store.set_roles(rec.id, (new_role,))
            updated += 1
    _authaudit.record(
        "user_role_assigned", tenant_id=tenant_id, principal_id=principal_id,
        action="roles:assign", result="ok", actor=actor.principal_id, role=new_role,
    )
    return updated


def list_users(
    tenant_id: str, *, actor: Principal, cred_store: CredentialStore | None = None
) -> list[dict]:
    """List the users in ``tenant_id`` (principals + roles + active state, no secrets).
    Tenant-admin only."""
    _require_tenant_admin(actor, tenant_id)
    return (cred_store or CredentialStore()).principals_for_tenant(tenant_id)


class BootstrapError(Exception):
    """The initial-admin bootstrap could not proceed (e.g. no credential supplied).
    Carries a machine ``reason``; the message is safe to show the operator."""

    def __init__(self, message: str, *, reason: str = "bootstrap_failed") -> None:
        super().__init__(message)
        self.reason = reason


def bootstrap_initial_admin(
    *,
    username: str | None = None,
    password: str | None = None,
    tenant_id: str | None = None,
    vault_id: str | None = None,
    cred_store: CredentialStore | None = None,
    provider: "providers.LocalPasswordProvider | None" = None,
    audit: SystemAuditLog | None = None,
    data_root: Path | str | None = None,
) -> dict:
    """Provision the **one initial admin** from configuration (R2): the admin principal
    (``role=admin``), its **tenant**, and a **default vault**, in the
    ``must_change_password`` state — so the bootstrap password is single-use in effect
    (it can do nothing but set a new one, R3).

    Inputs come from configuration/secret store (or the explicit args, which the CLI/
    entrypoint fills from ``MNESIS_ADMIN_USERNAME`` / ``MNESIS_ADMIN_PASSWORD`` /
    ``MNESIS_ADMIN_TENANT``). There is **no default password anywhere** — an absent or
    weak credential **fails** (`BootstrapError` / password policy), never defaults.

    **Idempotent + non-destructive:** if the target tenant already has an active admin
    this is a **NO-OP** — it never resets, re-enables, or changes an existing admin's
    password or role; the no-op is recorded in the system audit log. Both outcomes are
    audited (never the credential). Returns a dict describing what happened."""
    username = username or config.MNESIS_ADMIN_USERNAME
    tenant_id = tenant_id or config.MNESIS_ADMIN_TENANT
    vault_id = vault_id or config.DEFAULT_VAULT_ID
    # The credential comes from configuration/secret store when not passed explicitly
    # (an explicit empty/blank value is preserved so it fails the guard below, not defaults).
    if password is None:
        password = config.MNESIS_ADMIN_PASSWORD
    aud = audit or (SystemAuditLog((Path(data_root) / config.SYSTEM_AUDIT_FILENAME)) if data_root else SystemAuditLog())
    store = cred_store or CredentialStore(
        (Path(data_root) / config.CREDENTIALS_FILENAME) if data_root else None
    )

    # No-clobber: an existing active admin in the tenant → NO-OP (log clearly, change nothing).
    existing = _active_admin_ids(store, tenant_id)
    if existing:
        aud.record(
            "bootstrap_initial_admin_noop", tenant_id=tenant_id, actor=None,
            reason="admin_exists", existing_admins=sorted(existing),
        )
        return {
            "created": False, "reason": "admin_exists", "tenant_id": tenant_id,
            "existing_admins": sorted(existing),
        }

    # Refuse a weak/absent credential — no default exists anywhere (fail closed).
    if not password or not password.strip():
        raise BootstrapError(
            "no initial-admin password supplied: set MNESIS_ADMIN_PASSWORD (or pass "
            "--password) — there is NO default password. Bootstrap refused.",
            reason="no_credential",
        )
    providers.check_password_policy(password)  # raises PasswordPolicyError on a weak one

    # Provision the tenant + its default vault, then the admin — must_change_password.
    tenancy.create_tenant(tenant_id, data_root=data_root)
    if vault_id != config.DEFAULT_VAULT_ID:
        tenancy.create_vault(tenant_id, vault_id, owner_principal=username, data_root=data_root)
    prov = provider or providers.LocalPasswordProvider(store=store)
    rec = prov.register(tenant_id, username, "admin", password, must_change_password=True)
    aud.record(
        "bootstrap_initial_admin", tenant_id=tenant_id, actor=username,
        principal_id=username, credential_id=rec.id, role="admin", vault_id=vault_id,
        must_change_password=True,
    )
    return {
        "created": True, "tenant_id": tenant_id, "principal_id": username,
        "vault_id": vault_id, "credential_id": rec.id, "role": "admin",
        "must_change_password": True,
    }


def bootstrap_tenant_admin(
    tenant_id: str,
    principal_id: str,
    password: str,
    *,
    cred_store: CredentialStore | None = None,
    provider: "providers.LocalPasswordProvider | None" = None,
    data_root: Path | str | None = None,
) -> dict:
    """Backward-compatible shim over :func:`bootstrap_initial_admin` (R2): create
    ``tenant_id`` + its default vault and the first admin from an operator-supplied
    password, in the ``must_change_password`` state. Guarded/idempotent/no-clobber."""
    return bootstrap_initial_admin(
        username=principal_id, password=password, tenant_id=tenant_id,
        cred_store=cred_store, provider=provider, data_root=data_root,
    )


def create_system_admin(
    principal_id: str,
    password: str,
    *,
    admin: Principal,
    cred_store: CredentialStore | None = None,
    audit: SystemAuditLog | None = None,
) -> "auth.Credential":
    """Create an additional **system-admin** (password). Only an existing system-admin
    may do this; recorded in the SYSTEM audit log. No default password."""
    require_admin(admin)
    providers.check_password_policy(password)
    rec = (cred_store or CredentialStore()).issue_system_admin_password(
        principal_id, password, name="system-admin"
    )
    (audit or SystemAuditLog()).record(
        "create_system_admin", tenant_id=None, actor=admin.principal_id, credential_id=rec.id
    )
    return rec
