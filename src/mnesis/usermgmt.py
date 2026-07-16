"""R4 — the admin-only **user-management service** (per-user tenancy).

One service the Web UI (R5) and CLI (R6) both call, so both surfaces share identical
rules. It manages **accounts**, not data: every operation is PDP-authorized (requires the
`admin` role, R1), audited (actor · target · action · result — never a credential), and
escalation-safe. **The admin role authorizes account *management* only — it never grants
access to a user's tenant/vault *data*** (that stays isolated by construction, §16).

**Per-user tenancy.** A "user" is a principal that owns its **own tenant + default vault**
(`username == tenant_id == principal_id`). `create_user` provisions the tenant + vault and
mints the user's initial one-time credential (in the `must_change_password` state, R3),
returned **once** to the admin. Because the user's data lives in its own tenant, the
creating admin — in a different tenant — cannot read it (cross-tenant is structurally
impossible). **Removing a user's data** means deleting its tenant/vaults via the existing
lifecycle (`admin.delete_tenant` / `vaults.delete_vault`); deactivation only denies access.

**Safety rules (enforced here, so both surfaces inherit them):**
  * every op requires the `admin` role, checked at the PDP (never by the caller);
  * **no self-role-change** (a principal can never change its own role — no escalation);
  * the **last active admin** cannot be demoted / deactivated / revoked (no lockout);
  * **deactivation force-revokes** the principal's credentials *and* runtime tokens
    immediately (access denied at once), while **retaining data** (mirroring tenant suspend).
"""

from __future__ import annotations

import secrets
from pathlib import Path

from . import audit as _authaudit
from . import authz, config, identity, providers, tenancy, tokens
from .auth import CredentialStore, Principal
from .identity import ADMIN_ROLE, USER_ROLE, canonical_role


class UserManagementError(Exception):
    """A user-management operation was refused. Carries a machine ``reason``; the message
    is safe to show the operator (never a secret)."""

    def __init__(self, message: str, *, reason: str = "denied") -> None:
        super().__init__(message)
        self.reason = reason


# --- helpers ---------------------------------------------------------------


def _store(cred_store, data_root) -> CredentialStore:
    return cred_store or CredentialStore(
        (Path(data_root) / config.CREDENTIALS_FILENAME) if data_root else None
    )


def _require_admin(actor: Principal | None, action: str, perm: str) -> None:
    """Admin-only gate — **enforced by the PDP** (R1), never by the caller. Requires a
    real admin principal (the legacy no-principal-permits path does NOT apply to account
    management). Denies a non-admin with a clear reason."""
    if actor is None:
        raise UserManagementError("not authenticated: an admin is required", reason="no_principal")
    d = authz.decide(actor, perm, context={"tenant_id": actor.tenant_id})
    if not d.allowed:
        raise UserManagementError(
            f"{actor.principal_id!r} may not {action} (requires the admin role)", reason=d.reason
        )


def _managed_tenants(store: CredentialStore, data_root) -> list[str]:
    """The tenants this service manages — every real tenant with a credentialed principal
    (the per-user tenants), excluding the system boundary."""
    reg = tenancy.TenantRegistry(
        (Path(data_root) / config.REGISTRY_FILENAME) if data_root else None
    )
    return [t.tenant_id for t in reg.list() if t.tenant_id != identity.SYSTEM_TENANT]


def _active_admin_usernames(store: CredentialStore, data_root) -> set[str]:
    """Every username that is an **active admin** across the managed per-user tenants."""
    out: set[str] = set()
    for tid in _managed_tenants(store, data_root):
        for u in store.principals_for_tenant(tid):
            if u["active"] and any(canonical_role(r) == ADMIN_ROLE for r in u["roles"]):
                out.add(u["principal_id"])
    return out


def _refuse_last_admin(store: CredentialStore, data_root, username: str, op: str) -> None:
    admins = _active_admin_usernames(store, data_root)
    if admins == {username}:
        raise UserManagementError(
            f"refusing to {op} {username!r}: it is the last active admin (no-lockout rule)",
            reason="last_admin",
        )


def _password_record(store: CredentialStore, username: str):
    return store.find_password_credential(username, username)


def _must_change(store: CredentialStore, username: str) -> bool:
    rec = _password_record(store, username)
    return bool(rec and rec.must_change_password)


def _one_time_password() -> str:
    """A strong, single-use initial credential (well above the policy minimum)."""
    return secrets.token_urlsafe(18)


def _norm_role(role: str) -> str:
    r = canonical_role(role)
    if r not in {ADMIN_ROLE, USER_ROLE}:
        raise UserManagementError(f"invalid role {role!r}; one of admin, user", reason="invalid_role")
    return r


# --- operations (admin-only, PDP-authorized, audited) ----------------------


def create_user(
    actor: Principal,
    username: str,
    role: str = USER_ROLE,
    *,
    password: str | None = None,
    cred_store: CredentialStore | None = None,
    provider: "providers.LocalPasswordProvider | None" = None,
    data_root=None,
) -> dict:
    """Create a user (per-user tenancy): provision its **own tenant + default vault**, mint
    its initial **one-time** credential in the ``must_change_password`` state, and return it
    **once** to the admin (stored hashed; never logged). Admin-only; **rejects duplicates**;
    enforces the password policy. The role is one of ``admin``/``user``.

    Returns ``{username, tenant_id, vault_id, role, credential_id, initial_password,
    must_change_password}`` — ``initial_password`` is shown once (the caller must not log it)."""
    _require_admin(actor, "create users", authz.USERS_MANAGE)
    role = _norm_role(role)
    try:
        tenancy.validate_tenant_id(username)  # username == tenant id == principal id
    except tenancy.TenancyError as exc:
        raise UserManagementError(f"invalid username {username!r}", reason="invalid_username") from exc

    store = _store(cred_store, data_root)
    if store.principals_for_tenant(username):
        raise UserManagementError(f"user {username!r} already exists", reason="exists")

    pw = password or _one_time_password()
    providers.check_password_policy(pw)  # fail before any write on a weak (admin-supplied) one

    ctx = tenancy.create_tenant(username, data_root=data_root)  # tenant + default vault
    prov = provider or providers.LocalPasswordProvider(store=store)
    rec = prov.register(username, username, role, pw, must_change_password=True)
    _authaudit.record(
        "user_created", tenant_id=username, principal_id=username, credential_id=rec.id,
        action="users:manage", result="ok", actor=actor.principal_id, role=role,
        vault_id=ctx.vault_id,
    )
    return {
        "username": username, "tenant_id": username, "vault_id": ctx.vault_id, "role": role,
        "credential_id": rec.id, "initial_password": pw, "must_change_password": True,
    }


def list_users(
    actor: Principal, *, cred_store: CredentialStore | None = None, data_root=None
) -> list[dict]:
    """List the users in the admin's scope (the managed per-user tenants) — no secrets:
    each ``{username, role, active, must_change_password}``. Admin-only."""
    _require_admin(actor, "list users", authz.USERS_MANAGE)
    store = _store(cred_store, data_root)
    out: list[dict] = []
    for tid in _managed_tenants(store, data_root):
        for u in store.principals_for_tenant(tid):
            out.append({
                "username": u["principal_id"],
                "role": canonical_role(u["roles"][0]) if u["roles"] else USER_ROLE,
                "active": u["active"],
                "must_change_password": _must_change(store, u["principal_id"]),
            })
    return sorted(out, key=lambda x: x["username"])


def change_role(
    actor: Principal, username: str, role: str, *,
    cred_store: CredentialStore | None = None, data_root=None,
) -> int:
    """Change ``username``'s role (``admin``/``user``). Admin-only (`roles:assign`).
    Refuses a **self role-change** (no escalation) and demoting the **last active admin**
    (no-lockout). Audited. Returns the number of credentials updated."""
    _require_admin(actor, "assign roles", authz.ROLES_ASSIGN)
    role = _norm_role(role)
    if actor.principal_id == username:
        raise UserManagementError(
            f"{actor.principal_id!r} may not change its own role (no self-escalation)",
            reason="self_role_change",
        )
    store = _store(cred_store, data_root)
    if not store.principals_for_tenant(username):
        raise UserManagementError(f"unknown user {username!r}", reason="unknown_user")
    if role != ADMIN_ROLE and username in _active_admin_usernames(store, data_root):
        _refuse_last_admin(store, data_root, username, "demote")

    updated = 0
    for rec in store.list_for_tenant(username):
        if rec.principal_id == username:
            store.set_roles(rec.id, (role,))
            updated += 1
    _authaudit.record(
        "user_role_assigned", tenant_id=username, principal_id=username,
        action="roles:assign", result="ok", actor=actor.principal_id, role=role,
    )
    return updated


def deactivate_user(
    actor: Principal, username: str, *,
    cred_store: CredentialStore | None = None,
    token_service: "tokens.TokenService | None" = None, data_root=None,
) -> dict:
    """Deactivate ``username``: **force-revoke** every credential AND runtime token it holds
    (immediate everywhere), while **retaining its data** (its tenant/vaults are untouched —
    mirroring tenant suspend). Admin-only; refuses the **last active admin** (no-lockout).
    Audited. Removing the data is a separate lifecycle op (delete the tenant/vaults)."""
    _require_admin(actor, "deactivate users", authz.USERS_MANAGE)
    store = _store(cred_store, data_root)
    if not store.principals_for_tenant(username):
        raise UserManagementError(f"unknown user {username!r}", reason="unknown_user")
    _refuse_last_admin(store, data_root, username, "deactivate")
    n_creds = store.revoke_for_principal(username, username)
    n_tokens = (token_service or tokens.TokenService()).revoke_all_for_principal(username, username)
    _authaudit.record(
        "user_deactivated", tenant_id=username, principal_id=username, action="users:manage",
        result="ok", actor=actor.principal_id, credentials_revoked=n_creds, tokens_revoked=n_tokens,
    )
    return {"username": username, "credentials_revoked": n_creds, "tokens_revoked": n_tokens,
            "data_retained": True}


def reactivate_user(
    actor: Principal, username: str, *, password: str | None = None,
    cred_store: CredentialStore | None = None, data_root=None,
) -> dict:
    """Reactivate a deactivated user: restore its login with a **new one-time** credential
    in the ``must_change_password`` state (its old password is not recovered; runtime tokens
    revoked at deactivation stay dead). Admin-only, audited. Returns the one-time credential."""
    _require_admin(actor, "reactivate users", authz.USERS_MANAGE)
    store = _store(cred_store, data_root)
    rec = _password_record(store, username)
    if rec is None:
        raise UserManagementError(f"unknown user {username!r}", reason="unknown_user")
    pw = password or _one_time_password()
    providers.check_password_policy(pw)
    updated = store.set_password(rec.id, pw, must_change_password=True, clear_revocation=True)
    _authaudit.record(
        "user_reactivated", tenant_id=username, principal_id=username, credential_id=updated.id,
        action="users:manage", result="ok", actor=actor.principal_id,
    )
    return {"username": username, "credential_id": updated.id, "initial_password": pw,
            "must_change_password": True}


def reset_password(
    actor: Principal, username: str, *, password: str | None = None,
    cred_store: CredentialStore | None = None,
    token_service: "tokens.TokenService | None" = None, data_root=None,
) -> dict:
    """Reset ``username``'s password to a **new one-time** credential and force a change on
    next login (`must_change_password`, R3). Revokes the user's runtime tokens (old sessions
    die). Admin-only, audited. Returns the one-time credential (shown once, never logged)."""
    _require_admin(actor, "reset passwords", authz.USERS_MANAGE)
    store = _store(cred_store, data_root)
    rec = _password_record(store, username)
    if rec is None:
        raise UserManagementError(f"unknown user {username!r}", reason="unknown_user")
    pw = password or _one_time_password()
    providers.check_password_policy(pw)
    updated = store.set_password(rec.id, pw, must_change_password=True)
    (token_service or tokens.TokenService()).revoke_all_for_principal(username, username)
    _authaudit.record(
        "user_password_reset", tenant_id=username, principal_id=username, credential_id=updated.id,
        action="users:manage", result="ok", actor=actor.principal_id,
    )
    return {"username": username, "credential_id": updated.id, "initial_password": pw,
            "must_change_password": True}


def revoke_credentials(
    actor: Principal, username: str, *,
    cred_store: CredentialStore | None = None,
    token_service: "tokens.TokenService | None" = None, data_root=None,
) -> dict:
    """Force-revoke **all** of ``username``'s credentials and runtime tokens immediately
    (compromise response). Admin-only; refuses the **last active admin** (no-lockout).
    Audited. Data is retained (unlike a lifecycle delete)."""
    _require_admin(actor, "revoke credentials", authz.CREDENTIALS_ISSUE)
    store = _store(cred_store, data_root)
    if not store.principals_for_tenant(username):
        raise UserManagementError(f"unknown user {username!r}", reason="unknown_user")
    _refuse_last_admin(store, data_root, username, "revoke the credentials of")
    n_creds = store.revoke_for_principal(username, username)
    n_tokens = (token_service or tokens.TokenService()).revoke_all_for_principal(username, username)
    _authaudit.record(
        "user_credentials_revoked", tenant_id=username, principal_id=username, action="credentials:issue",
        result="ok", actor=actor.principal_id, credentials_revoked=n_creds, tokens_revoked=n_tokens,
    )
    return {"username": username, "credentials_revoked": n_creds, "tokens_revoked": n_tokens}
