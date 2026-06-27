"""Authentication — credentials → (TenantContext, Principal), resolved at boundaries.

This replaces the single global MCP token with **tenant- and principal-scoped
credentials**. A credential resolves to ``{tenant_id, principal_id, role}``; the
tenant is taken **only** from the validated credential — never from a request body,
header, path, or content — and an absent/invalid/expired/revoked credential is
**denied** (fail closed, with no default-tenant fallback).

Security posture:
  - **Secrets at rest are hashed.** The raw opaque token is returned by
    :meth:`CredentialStore.issue` exactly once and is **never stored or logged**;
    the store keeps only ``sha256(pepper || token)``. (Tokens are high-entropy
    random strings, not user passwords, so a fast hash + optional server pepper is
    the right primitive; an attacker who steals the store cannot recover a token.)
  - **Validation is constant-time.** Lookups compare token hashes with
    :func:`hmac.compare_digest`.
  - **Never logged.** No function here logs a token or a hash.

The credential store lives **outside any tenant root** (beside the tenant registry),
so it is itself not reachable through a tenant. The boundary resolves the active
``(TenantContext, Principal)`` here; the tenant binding then makes the store
tenant-scoped exactly as in §16.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import config, tenancy
from .tenancy import TenantContext, validate_tenant_id

#: The roles a *tenant* principal may hold. Authorization (what each role may do)
#: is layered in `authz.py`.
ROLES: frozenset[str] = frozenset({"admin", "member", "readonly", "agent"})

#: The SYSTEM-ADMIN boundary (T7): a system admin is **not** a tenant principal. Its
#: credential carries a reserved, non-tenant id (which can never collide with a real
#: tenant id — `validate_tenant_id` rejects a leading ``_``) and a reserved role. It
#: manages tenant lifecycle and can never act as a tenant member; a tenant principal
#: can never manage tenants.
SYSTEM_TENANT: str = "__system__"
SYSTEM_ROLE: str = "system_admin"


class AuthError(Exception):
    """Base class for authentication faults."""


class InvalidCredential(AuthError):
    """The credential is absent, malformed, unknown, expired, or revoked (deny)."""


class InvalidRole(AuthError, ValueError):
    """A role outside :data:`ROLES`."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _now_epoch() -> float:
    return datetime.now(timezone.utc).timestamp()


def validate_role(role: str) -> str:
    if role not in ROLES:
        raise InvalidRole(f"invalid role {role!r}; one of {sorted(ROLES)}")
    return role


def hash_token(raw: str) -> str:
    """A stable, non-reversible fingerprint of an opaque token: ``sha256(pepper||token)``.

    The optional pepper (``MNESIS_AUTH_PEPPER``) is a server-side secret that makes a
    stolen credential store useless without it. Never logged."""
    pepper = config.MNESIS_AUTH_PEPPER or ""
    return hashlib.sha256((pepper + (raw or "")).encode("utf-8")).hexdigest()


# --- Models ----------------------------------------------------------------


@dataclass(frozen=True)
class Principal:
    """An authenticated actor: who they are, which tenant, and their role."""

    principal_id: str
    tenant_id: str
    role: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Credential:
    """A stored credential record — **no raw token**, only its hash.

    ``id`` is a non-secret handle (for revocation/audit); ``token_hash`` is the only
    representation of the secret kept at rest.
    """

    id: str
    token_hash: str
    tenant_id: str
    principal_id: str
    role: str
    created: str
    expires_at: float | None = None  # epoch seconds; None = no expiry
    revoked: bool = False
    name: str | None = None  # optional human label (e.g. "ci-bot")

    def is_active(self, now: float | None = None) -> bool:
        now = now if now is not None else _now_epoch()
        if self.revoked:
            return False
        if self.expires_at is not None and now >= self.expires_at:
            return False
        return True

    def principal(self) -> Principal:
        return Principal(principal_id=self.principal_id, tenant_id=self.tenant_id, role=self.role)

    def public_dict(self) -> dict:
        """A safe view for listing/audit — excludes the token hash."""
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "principal_id": self.principal_id,
            "role": self.role,
            "created": self.created,
            "expires_at": self.expires_at,
            "revoked": self.revoked,
            "name": self.name,
        }


# --- Credential store (outside any tenant root) ----------------------------


class CredentialStore:
    """Issues, validates, and revokes credentials. JSON at
    ``DATA_ROOT/credentials.json`` (beside the tenant registry), holding only hashed
    tokens. Not reachable through any tenant."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else config.credentials_path()

    def _load(self) -> dict[str, dict]:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except (ValueError, OSError):
            return {}
        creds = data.get("credentials")
        return creds if isinstance(creds, dict) else {}

    def _save(self, creds: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({"credentials": creds}, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    # -- issue / revoke -----------------------------------------------------

    def issue(
        self,
        tenant_id: str,
        principal_id: str,
        role: str,
        *,
        expires_at: float | None = None,
        name: str | None = None,
    ) -> tuple[str, Credential]:
        """Mint a credential for ``tenant_id`` + ``principal_id`` with ``role``.

        Returns ``(raw_token, credential)``. The **raw token is returned once** and
        never stored or logged — only its hash is persisted. (T7 wraps this as the
        admin issuing API.)"""
        validate_tenant_id(tenant_id)
        validate_role(role)
        if not principal_id or "/" in principal_id or "\\" in principal_id:
            raise AuthError(f"invalid principal id: {principal_id!r}")
        raw = secrets.token_urlsafe(32)
        cred = Credential(
            id=secrets.token_hex(8),
            token_hash=hash_token(raw),
            tenant_id=tenant_id,
            principal_id=principal_id,
            role=role,
            created=_now_iso(),
            expires_at=expires_at,
            revoked=False,
            name=name,
        )
        creds = self._load()
        creds[cred.id] = asdict(cred)
        self._save(creds)
        return raw, cred

    def issue_system_admin(
        self, principal_id: str, *, expires_at: float | None = None, name: str | None = None
    ) -> tuple[str, Credential]:
        """Mint a **system-admin** credential (T7) — not tied to any tenant. Returns
        ``(raw_token, credential)``; the raw token is returned once and never stored.
        This is the lifecycle root-of-trust (bootstrapped locally by the operator)."""
        if not principal_id or "/" in principal_id or "\\" in principal_id:
            raise AuthError(f"invalid principal id: {principal_id!r}")
        raw = secrets.token_urlsafe(32)
        cred = Credential(
            id=secrets.token_hex(8),
            token_hash=hash_token(raw),
            tenant_id=SYSTEM_TENANT,
            principal_id=principal_id,
            role=SYSTEM_ROLE,
            created=_now_iso(),
            expires_at=expires_at,
            revoked=False,
            name=name,
        )
        creds = self._load()
        creds[cred.id] = asdict(cred)
        self._save(creds)
        return raw, cred

    def remove_tenant(self, tenant_id: str) -> int:
        """Delete every credential belonging to ``tenant_id`` (lifecycle delete, T7).
        Returns the number removed."""
        creds = self._load()
        doomed = [cid for cid, r in creds.items() if r.get("tenant_id") == tenant_id]
        for cid in doomed:
            del creds[cid]
        if doomed:
            self._save(creds)
        return len(doomed)

    def revoke(self, credential_id: str) -> bool:
        """Revoke a credential by id. Returns True if it transitioned to revoked."""
        creds = self._load()
        rec = creds.get(credential_id)
        if rec is None or rec.get("revoked"):
            return False
        rec["revoked"] = True
        creds[credential_id] = rec
        self._save(creds)
        return True

    # -- lookup -------------------------------------------------------------

    def get(self, credential_id: str) -> Credential | None:
        rec = self._load().get(credential_id)
        return Credential(**rec) if rec is not None else None

    def list_for_tenant(self, tenant_id: str) -> list[Credential]:
        validate_tenant_id(tenant_id)
        return sorted(
            (Credential(**r) for r in self._load().values() if r.get("tenant_id") == tenant_id),
            key=lambda c: c.created,
        )

    def validate(self, raw_token: str | None) -> Credential | None:
        """Return the active :class:`Credential` for ``raw_token``, or ``None``.

        Constant-time: hashes the presented token and compares against every stored
        hash with :func:`hmac.compare_digest`. An expired/revoked match returns None.
        """
        if not raw_token:
            return None
        presented = hash_token(raw_token)
        match: Credential | None = None
        for rec in self._load().values():
            if hmac.compare_digest(rec.get("token_hash", ""), presented):
                match = Credential(**rec)
        if match is None or not match.is_active():
            return None
        return match


# --- The resolver (the boundary entry point) -------------------------------


def resolve_principal(
    credential: str | None,
    *,
    store: CredentialStore | None = None,
    data_root: Path | str | None = None,
) -> tuple[TenantContext, Principal]:
    """Resolve an opaque ``credential`` to ``(TenantContext, Principal)``.

    **Fail closed:** an absent/invalid/expired/revoked credential raises
    :class:`InvalidCredential` — there is no default tenant. The tenant is taken
    **only** from the validated credential; any client-supplied tenant id is ignored
    by construction (this function never reads one)."""
    cred = (store or CredentialStore()).validate(credential)
    if cred is None:
        raise InvalidCredential("credential is absent, invalid, expired, or revoked")
    # A system-admin credential is NOT a tenant principal — never resolve it as one.
    if cred.tenant_id == SYSTEM_TENANT or cred.role == SYSTEM_ROLE:
        raise InvalidCredential("system-admin credential is not a tenant principal")
    # A suspended tenant denies access while RETAINING its data (T7). Fail closed.
    tenant = _registry_for(data_root).get(cred.tenant_id)
    if tenant is not None and tenant.status != "active":
        raise InvalidCredential(f"tenant {cred.tenant_id!r} is {tenant.status} — access denied")
    ctx = tenancy.open_tenant(cred.tenant_id, data_root=data_root)
    return ctx, cred.principal()


def _registry_for(data_root: Path | str | None) -> "tenancy.TenantRegistry":
    if data_root is None:
        return tenancy.TenantRegistry()
    return tenancy.TenantRegistry(Path(data_root) / config.REGISTRY_FILENAME)


def resolve_admin(
    credential: str | None, *, store: CredentialStore | None = None
) -> Principal:
    """Resolve a credential to a **system-admin** :class:`Principal` (T7), or deny.

    Fail closed: a credential that is absent/invalid/expired/revoked — or that is a
    *tenant* credential rather than a system-admin one — raises
    :class:`InvalidCredential`. A system admin is the only actor allowed to manage
    tenant lifecycle, and is never a member of any tenant."""
    cred = (store or CredentialStore()).validate(credential)
    if cred is None or cred.tenant_id != SYSTEM_TENANT or cred.role != SYSTEM_ROLE:
        raise InvalidCredential("not a valid system-admin credential")
    return cred.principal()


def is_system_admin(principal: "Principal | None") -> bool:
    return (
        isinstance(principal, Principal)
        and principal.tenant_id == SYSTEM_TENANT
        and principal.role == SYSTEM_ROLE
    )


# --- Active principal binding (alongside the active tenant) -----------------

_active_principal: ContextVar[Principal | None] = ContextVar("mnesis_active_principal", default=None)


def current_principal() -> Principal:
    """The authenticated principal, or raise if none is bound (fail closed)."""
    p = _active_principal.get()
    if p is None:
        raise AuthError("no authenticated principal bound at this boundary")
    return p


def current_principal_or_none() -> Principal | None:
    return _active_principal.get()


def bind_principal(principal: Principal) -> Token:
    if not isinstance(principal, Principal):
        raise TypeError(f"bind_principal needs a Principal, got {type(principal).__name__}")
    return _active_principal.set(principal)


def unbind_principal(token: Token) -> None:
    _active_principal.reset(token)


@contextmanager
def authenticated(
    credential: str | None,
    *,
    store: CredentialStore | None = None,
    data_root: Path | str | None = None,
):
    """Resolve ``credential`` and bind BOTH the tenant and the principal for the
    block. Denies (raises :class:`InvalidCredential`) on an unresolved credential."""
    ctx, principal = resolve_principal(credential, store=store, data_root=data_root)
    with tenancy.use(ctx):
        token = bind_principal(principal)
        try:
            yield ctx, principal
        finally:
            unbind_principal(token)
