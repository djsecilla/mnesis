"""Identity core (IAM1) — the shared principal / credential / role foundation.

This is the **one** identity model every surface (MCP, Web API, CLI, agents) and the
future policy-decision point (PDP) resolve through. It supersedes the minimal T3
credential store (``auth.py``), keeping T3's load-bearing guarantee — **the tenant is
taken solely from the verified credential** — while widening the model:

  - a richer **principal**: ``kind`` (human | agent) and ``status``;
  - first-class **roles → permissions**, and free-form **scopes**;
  - **credential records** carrying a secret *type* (opaque token or password),
    an at-rest **hash algorithm** (fast ``sha256`` for high-entropy tokens,
    **argon2id** for passwords), scopes, expiry, and a ``revoked_at`` timestamp;
  - a single resolver — :func:`resolve` — that turns a bearer credential into an
    :class:`AuthenticatedPrincipal` (tenant + identity + roles + scopes) or **denies**.

Security posture (unchanged from and extended beyond T3):
  - **Secrets are hashed at rest, never stored or logged in the clear.** A minted
    token's raw value is returned exactly once. Tokens are hashed with
    ``sha256(pepper || token)`` (they are high-entropy random strings, so a fast keyed
    hash suffices and enables constant-time lookup); passwords — which are low-entropy
    — are hashed with **argon2id**, salted per record.
  - **Fail closed.** An absent / malformed / unknown / expired / revoked credential —
    or a system-admin credential presented as a tenant principal, or a credential for
    a suspended tenant — is denied (:class:`Deny`). There is no default-tenant fallback
    and no path that reads a tenant id from anywhere but the credential record.
  - **Constant-time** token comparison via :func:`hmac.compare_digest`.

The credential store lives **outside any tenant root** (``DATA_ROOT/credentials.json``,
beside the tenant registry), so it is not reachable through a tenant. This module holds
**no surface-specific logic** — it is the shared core the surfaces call into.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import config, tenancy
from .config import now_iso as _now_iso
from .tenancy import TenantContext, validate_tenant_id

# --- Vocabulary: permissions, roles, kinds, statuses, credential/secret types ---

#: A permission is an atomic capability string. These mirror the coarse actions the
#: surfaces gate on; the PDP (a later prompt) consumes the role → permission mapping.
Permission = str
READ: Permission = "read"
WRITE: Permission = "write"
MAINTAIN: Permission = "maintain"  # decay / rebuild / graph-lint
ADMIN: Permission = "admin"  # tenant + credential administration
PERMISSIONS: frozenset[str] = frozenset({READ, WRITE, MAINTAIN, ADMIN})

#: A scope is a free-form, opaque capability token carried on a credential and
#: propagated onto the resolved principal (e.g. ``"mnesis:read"``). Scopes further
#: *narrow* what a credential may do; the PDP intersects them with role permissions.
Scope = str


@dataclass(frozen=True)
class Role:
    """A named bundle of :data:`Permission`."""

    name: str
    permissions: frozenset[str]

    def grants(self, permission: str) -> bool:
        return permission in self.permissions


#: The **two canonical account roles** (R1) a human principal holds within a tenant:
#:   - ``admin`` — account management (manage users, assign roles, issue/revoke
#:     credentials) **plus** everything a ``user`` may do in the admin's OWN
#:     tenant/vaults. It is a *user-management* role, **not** a data-access grant:
#:     an admin gains no read/write access to another principal's tenant or vault
#:     data — tenant/vault isolation is enforced by the PDP exactly as for a user.
#:   - ``user`` — its own vaults/knowledge (read/write) and its own password only.
#: ``member`` is a retained **alias of ``user``** (same permission set) so pre-R1
#: principals and callers keep working; ``agent`` (non-human machine principals) and
#: ``readonly`` are retained specialised roles. Role→permission for the PDP lives in
#: :mod:`mnesis.authz`; these coarse sets back :func:`permissions_for`.
ADMIN_ROLE: str = "admin"
USER_ROLE: str = "user"

BUILTIN_ROLES: dict[str, Role] = {
    "admin": Role("admin", frozenset({READ, WRITE, MAINTAIN, ADMIN})),
    "user": Role("user", frozenset({READ, WRITE, MAINTAIN})),
    "member": Role("member", frozenset({READ, WRITE, MAINTAIN})),  # alias of `user`
    "agent": Role("agent", frozenset({READ, WRITE, MAINTAIN})),
    "readonly": Role("readonly", frozenset({READ})),
}

#: Legacy role names → their canonical two-role equivalent (lossless migration): a
#: stored/`member` principal *is* a `user`. Names not listed pass through unchanged
#: (``admin``/``user`` are already canonical; ``agent``/``readonly`` are retained).
ROLE_ALIASES: dict[str, str] = {"member": USER_ROLE}

#: The two canonical account roles (R1). Used for display/normalisation; the store
#: still accepts every name in :data:`ROLES` (so no principal is ever locked out by
#: the migration).
CANONICAL_ROLES: frozenset[str] = frozenset({ADMIN_ROLE, USER_ROLE})


def canonical_role(name: str) -> str:
    """Map a (possibly legacy) role name to its canonical two-role equivalent
    (``member`` → ``user``); other names pass through. Lossless + idempotent."""
    return ROLE_ALIASES.get(name, name)


#: The role *names* a tenant principal may hold (backward-compatible with T3's ``ROLES``).
ROLES: frozenset[str] = frozenset(BUILTIN_ROLES)

#: The SYSTEM-ADMIN boundary (T7): a system admin is **not** a tenant principal. Its
#: credential carries a reserved, non-tenant id (which can never collide with a real
#: tenant id — ``validate_tenant_id`` rejects a leading ``_``) and a reserved role.
SYSTEM_TENANT: str = "__system__"
SYSTEM_ROLE: str = "system_admin"
_SYSTEM_ROLE_DEF: Role = Role(SYSTEM_ROLE, frozenset({ADMIN}))

#: Priority order used to pick a single "primary" role for backward-compatible callers
#: that still read a scalar ``.role`` (authz.py, the CLI, admin.py).
_ROLE_PRIORITY: tuple[str, ...] = ("admin", "user", "member", "agent", "readonly", SYSTEM_ROLE)

#: Principal kind — a human operator or a non-human agent identity.
HUMAN: str = "human"
AGENT: str = "agent"
KINDS: frozenset[str] = frozenset({HUMAN, AGENT})

#: Principal status.
ACTIVE: str = "active"
SUSPENDED: str = "suspended"
PRINCIPAL_STATUSES: frozenset[str] = frozenset({ACTIVE, SUSPENDED})

#: Credential secret types and their at-rest hash algorithms.
SECRET_TOKEN: str = "token"  # opaque high-entropy bearer token -> sha256(pepper||token)
SECRET_PASSWORD: str = "password"  # low-entropy human secret -> argon2id (salted)
SECRET_TYPES: frozenset[str] = frozenset({SECRET_TOKEN, SECRET_PASSWORD})
ALGO_SHA256: str = "sha256"
ALGO_ARGON2ID: str = "argon2id"


# --- Errors ----------------------------------------------------------------


class AuthError(Exception):
    """Base class for authentication faults."""


class InvalidCredential(AuthError):
    """The credential is absent, malformed, unknown, expired, or revoked."""


class Deny(InvalidCredential):
    """The resolver refused a credential (fail closed).

    A subclass of :class:`InvalidCredential` (and thus :class:`AuthError`) so every
    existing ``except AuthError`` / ``except InvalidCredential`` boundary keeps
    treating a denial as a denial. Carries an optional machine ``reason``.
    """

    def __init__(self, message: str, *, reason: str = "invalid") -> None:
        super().__init__(message)
        self.reason = reason


class InvalidRole(AuthError, ValueError):
    """A role outside :data:`ROLES`."""


def validate_role(role: str) -> str:
    if role not in ROLES:
        raise InvalidRole(f"invalid role {role!r}; one of {sorted(ROLES)}")
    return role


def _now_epoch() -> float:
    return datetime.now(timezone.utc).timestamp()


def _primary_role(roles: frozenset[str] | tuple[str, ...] | set[str]) -> str:
    """Pick one representative role for scalar ``.role`` consumers (fail-safe)."""
    rs = set(roles)
    for name in _ROLE_PRIORITY:
        if name in rs:
            return name
    return next(iter(sorted(rs)), "readonly")


def permissions_for(roles) -> frozenset[str]:
    """Union of the permissions granted by ``roles`` (unknown names contribute none)."""
    out: set[str] = set()
    for name in roles:
        role = BUILTIN_ROLES.get(name) or (_SYSTEM_ROLE_DEF if name == SYSTEM_ROLE else None)
        if role is not None:
            out |= role.permissions
    return frozenset(out)


# --- Secret hashing --------------------------------------------------------


def hash_token(raw: str) -> str:
    """A stable, non-reversible fingerprint of an opaque token: ``sha256(pepper||token)``.

    The optional pepper (``MNESIS_AUTH_PEPPER``) is a server-side secret that makes a
    stolen credential store useless without it. Never logged."""
    pepper = config.MNESIS_AUTH_PEPPER or ""
    return hashlib.sha256((pepper + (raw or "")).encode("utf-8")).hexdigest()


def _password_hasher():
    """Lazily construct an argon2id hasher (argon2-cffi). Kept lazy so the offline
    token path — the only one the PoC exercises by default — needs no extra dependency."""
    try:
        from argon2 import PasswordHasher
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise AuthError(
            "password credentials require argon2-cffi; install it (it ships in the "
            "mnesis dependencies) to hash/verify passwords"
        ) from exc
    return PasswordHasher()


def hash_password(raw: str) -> str:
    """Hash a (low-entropy) password with **argon2id**, salted per call. The pepper is
    mixed in for defense in depth. Never logged; the plaintext is never stored."""
    if not raw:
        raise AuthError("refusing to hash an empty password")
    return _password_hasher().hash((config.MNESIS_AUTH_PEPPER or "") + raw)


def verify_password(stored_hash: str, raw: str) -> bool:
    """Constant-time-ish argon2id verification; any error (bad hash, mismatch) → False."""
    if not stored_hash or not raw:
        return False
    try:
        return bool(_password_hasher().verify(stored_hash, (config.MNESIS_AUTH_PEPPER or "") + raw))
    except Exception:
        return False


# --- Models: User, Principal, AuthenticatedPrincipal -----------------------


@dataclass(frozen=True)
class User:
    """A persistent identity record — *who* an actor is, independent of any session.

    The credential store references a user by ``(tenant_id, principal_id)``; a
    dedicated user store is a later prompt, so this is the shape the rest of the IAM
    stack agrees on now."""

    principal_id: str
    tenant_id: str
    kind: str = HUMAN
    status: str = ACTIVE

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Principal:
    """The bound actor: identity + effective role/scopes for the current context.

    Backward-compatible with T3 — the first three positional fields are unchanged
    (``principal_id, tenant_id, role``) and ``.role`` stays a scalar — while gaining
    ``kind``/``status`` and the richer ``roles``/``scopes`` sets used by the PDP."""

    principal_id: str
    tenant_id: str
    role: str = "readonly"
    kind: str = HUMAN
    status: str = ACTIVE
    roles: frozenset[str] = field(default_factory=frozenset)
    scopes: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        # A principal always carries at least its scalar role in the role set.
        if not self.roles:
            object.__setattr__(self, "roles", frozenset({self.role}))
        object.__setattr__(self, "scopes", frozenset(self.scopes))

    @property
    def permissions(self) -> frozenset[str]:
        return permissions_for(self.roles)

    def has_permission(self, permission: str) -> bool:
        return permission in self.permissions

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    def to_dict(self) -> dict:
        return {
            "principal_id": self.principal_id,
            "tenant_id": self.tenant_id,
            "role": self.role,
            "kind": self.kind,
            "status": self.status,
            "roles": sorted(self.roles),
            "scopes": sorted(self.scopes),
        }


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    """The result of authenticating a credential for a request: the tenant, the
    principal, and the roles/scopes the credential grants. This is the type the single
    :func:`resolve` interface returns; :meth:`to_principal` yields the bound actor."""

    tenant_id: str
    principal_id: str
    roles: frozenset[str]
    scopes: frozenset[str] = field(default_factory=frozenset)
    kind: str = HUMAN

    def __post_init__(self) -> None:
        object.__setattr__(self, "roles", frozenset(self.roles))
        object.__setattr__(self, "scopes", frozenset(self.scopes))

    @property
    def role(self) -> str:
        """A single representative role, for scalar-role consumers."""
        return _primary_role(self.roles)

    @property
    def permissions(self) -> frozenset[str]:
        return permissions_for(self.roles)

    def has_role(self, role: str) -> bool:
        return role in self.roles

    def has_permission(self, permission: str) -> bool:
        return permission in self.permissions

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    def to_principal(self) -> Principal:
        return Principal(
            principal_id=self.principal_id,
            tenant_id=self.tenant_id,
            role=self.role,
            kind=self.kind,
            roles=self.roles,
            scopes=self.scopes,
        )

    def to_dict(self) -> dict:
        return {
            "tenant_id": self.tenant_id,
            "principal_id": self.principal_id,
            "roles": sorted(self.roles),
            "scopes": sorted(self.scopes),
            "kind": self.kind,
        }


# --- Credential record -----------------------------------------------------


@dataclass(frozen=True)
class CredentialRecord:
    """A stored credential — **never the raw secret**, only its hash.

    ``id`` is a non-secret handle (revocation/audit). ``secret_hash`` is the only
    representation of the secret kept at rest, hashed per ``hash_algo`` for the
    ``secret_type``. The on-disk schema is a superset of T3's; :meth:`from_dict`
    migrates legacy rows (``token_hash``/``role``/``revoked``) transparently."""

    id: str
    secret_hash: str
    tenant_id: str
    principal_id: str
    roles: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()
    kind: str = HUMAN
    secret_type: str = SECRET_TOKEN
    hash_algo: str = ALGO_SHA256
    created: str = ""
    expires_at: float | None = None  # epoch seconds; None = no expiry
    revoked_at: str | None = None  # ISO timestamp when revoked; None = active
    name: str | None = None  # optional human label (e.g. "ci-bot")
    #: R2/R3: a password credential the principal MUST rotate before it grants anything
    #: beyond a password change (set on the bootstrapped initial admin; cleared on reset).
    must_change_password: bool = False

    # -- backward-compatible scalar views (T3 shape) --------------------------
    @property
    def role(self) -> str:
        return _primary_role(self.roles)

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def token_hash(self) -> str:  # legacy alias
        return self.secret_hash

    # -- lifecycle ------------------------------------------------------------
    def is_active(self, now: float | None = None) -> bool:
        now = now if now is not None else _now_epoch()
        if self.revoked_at is not None:
            return False
        if self.expires_at is not None and now >= self.expires_at:
            return False
        return True

    def principal(self) -> Principal:
        return Principal(
            principal_id=self.principal_id,
            tenant_id=self.tenant_id,
            role=self.role,
            kind=self.kind,
            roles=frozenset(self.roles),
            scopes=frozenset(self.scopes),
        )

    def authenticated(self) -> AuthenticatedPrincipal:
        return AuthenticatedPrincipal(
            tenant_id=self.tenant_id,
            principal_id=self.principal_id,
            roles=frozenset(self.roles),
            scopes=frozenset(self.scopes),
            kind=self.kind,
        )

    def public_dict(self) -> dict:
        """A safe view for listing/audit — excludes the secret hash entirely."""
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "principal_id": self.principal_id,
            "role": self.role,
            "roles": list(self.roles),
            "scopes": list(self.scopes),
            "kind": self.kind,
            "secret_type": self.secret_type,
            "created": self.created,
            "expires_at": self.expires_at,
            "revoked": self.revoked,
            "revoked_at": self.revoked_at,
            "name": self.name,
            "must_change_password": self.must_change_password,
        }

    def to_dict(self) -> dict:
        """The full on-disk record (rich schema). Lists, not sets, for JSON."""
        d = asdict(self)
        d["roles"] = list(self.roles)
        d["scopes"] = list(self.scopes)
        return d

    @classmethod
    def from_dict(cls, d: dict, *, id_hint: str | None = None) -> "CredentialRecord":
        """Build a record from a stored dict, **migrating T3's legacy schema**.

        Legacy rows carried ``token_hash`` / ``role`` (scalar) / ``revoked`` (bool) and
        no ``scopes``/``kind``/``secret_type``. We map them forward so existing access
        is preserved without a migration step (the store also offers an explicit
        :meth:`IdentityStore.migrate` to rewrite the file)."""
        secret_hash = d.get("secret_hash") or d.get("token_hash") or ""
        # roles: prefer the new list; else the legacy scalar; else none.
        roles = d.get("roles")
        if roles:
            roles_t = tuple(roles)
        elif d.get("role"):
            roles_t = (d["role"],)
        else:
            roles_t = ()
        # revoked_at: prefer the new timestamp; else derive from the legacy bool.
        revoked_at = d.get("revoked_at")
        if revoked_at is None and d.get("revoked"):
            revoked_at = d.get("created") or "1970-01-01T00:00:00.000000Z"
        kind = d.get("kind") or (AGENT if "agent" in roles_t else HUMAN)
        return cls(
            id=d.get("id") or id_hint or "",
            secret_hash=secret_hash,
            tenant_id=d["tenant_id"],
            principal_id=d["principal_id"],
            roles=roles_t,
            scopes=tuple(d.get("scopes") or ()),
            kind=kind,
            secret_type=d.get("secret_type", SECRET_TOKEN),
            hash_algo=d.get("hash_algo", ALGO_SHA256),
            created=d.get("created", ""),
            expires_at=d.get("expires_at"),
            revoked_at=revoked_at,
            name=d.get("name"),
            must_change_password=bool(d.get("must_change_password", False)),
        )


# --- The credential store (outside any tenant root) ------------------------


class IdentityStore:
    """Issues, validates, and revokes credentials. JSON at ``DATA_ROOT/credentials.json``
    (beside the tenant registry), holding only hashed secrets. Not reachable through any
    tenant. This is the T3 ``CredentialStore`` widened to the IAM1 record schema; the
    old file layout loads transparently (see :meth:`CredentialRecord.from_dict`)."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else config.credentials_path()

    # -- persistence ----------------------------------------------------------
    def _load(self) -> dict[str, CredentialRecord]:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except (ValueError, OSError):
            return {}
        raw = data.get("credentials")
        if not isinstance(raw, dict):
            return {}
        out: dict[str, CredentialRecord] = {}
        for cid, rec in raw.items():
            try:
                out[cid] = CredentialRecord.from_dict(rec, id_hint=cid)
            except (KeyError, TypeError):
                continue
        return out

    def _save(self, records: dict[str, CredentialRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"credentials": {cid: r.to_dict() for cid, r in records.items()}}
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    def _validate_principal_id(self, principal_id: str) -> None:
        if not principal_id or "/" in principal_id or "\\" in principal_id:
            raise AuthError(f"invalid principal id: {principal_id!r}")

    # -- issue ----------------------------------------------------------------
    def issue(
        self,
        tenant_id: str,
        principal_id: str,
        role: str,
        *,
        expires_at: float | None = None,
        name: str | None = None,
        scopes: tuple[str, ...] | list[str] | None = None,
        kind: str | None = None,
    ) -> tuple[str, CredentialRecord]:
        """Mint an **opaque token** credential for ``tenant_id`` + ``principal_id`` with
        ``role``. Returns ``(raw_token, record)`` — the raw token is returned **once**
        and never stored or logged. (T3-compatible signature; ``scopes``/``kind`` are
        the IAM1 additions.)"""
        validate_tenant_id(tenant_id)
        validate_role(role)
        self._validate_principal_id(principal_id)
        raw = secrets.token_urlsafe(32)
        rec = CredentialRecord(
            id=secrets.token_hex(8),
            secret_hash=hash_token(raw),
            tenant_id=tenant_id,
            principal_id=principal_id,
            roles=(role,),
            scopes=tuple(scopes or ()),
            kind=kind or (AGENT if role == "agent" else HUMAN),
            secret_type=SECRET_TOKEN,
            hash_algo=ALGO_SHA256,
            created=_now_iso(),
            expires_at=expires_at,
            name=name,
        )
        records = self._load()
        records[rec.id] = rec
        self._save(records)
        return raw, rec

    def issue_password(
        self,
        tenant_id: str,
        principal_id: str,
        role: str,
        password: str,
        *,
        expires_at: float | None = None,
        name: str | None = None,
        scopes: tuple[str, ...] | list[str] | None = None,
        kind: str | None = None,
        must_change_password: bool = False,
    ) -> CredentialRecord:
        """Mint a **password** credential (hashed with argon2id at rest). Unlike a token
        there is no returned secret — the caller supplied it. Verify a login with
        :meth:`verify_login`. ``must_change_password`` marks a credential (e.g. the
        bootstrapped initial admin) that the principal must rotate before it grants
        anything beyond a password change (R2/R3)."""
        validate_tenant_id(tenant_id)
        validate_role(role)
        self._validate_principal_id(principal_id)
        rec = CredentialRecord(
            id=secrets.token_hex(8),
            secret_hash=hash_password(password),
            tenant_id=tenant_id,
            principal_id=principal_id,
            roles=(role,),
            scopes=tuple(scopes or ()),
            kind=kind or HUMAN,
            secret_type=SECRET_PASSWORD,
            hash_algo=ALGO_ARGON2ID,
            created=_now_iso(),
            expires_at=expires_at,
            name=name,
            must_change_password=must_change_password,
        )
        records = self._load()
        records[rec.id] = rec
        self._save(records)
        return rec

    def issue_system_admin(
        self, principal_id: str, *, expires_at: float | None = None, name: str | None = None
    ) -> tuple[str, CredentialRecord]:
        """Mint a **system-admin** token credential (T7) — not tied to any tenant."""
        self._validate_principal_id(principal_id)
        raw = secrets.token_urlsafe(32)
        rec = CredentialRecord(
            id=secrets.token_hex(8),
            secret_hash=hash_token(raw),
            tenant_id=SYSTEM_TENANT,
            principal_id=principal_id,
            roles=(SYSTEM_ROLE,),
            kind=HUMAN,
            secret_type=SECRET_TOKEN,
            hash_algo=ALGO_SHA256,
            created=_now_iso(),
            expires_at=expires_at,
            name=name,
        )
        records = self._load()
        records[rec.id] = rec
        self._save(records)
        return raw, rec

    def issue_system_admin_password(
        self, principal_id: str, password: str, *, expires_at: float | None = None, name: str | None = None
    ) -> CredentialRecord:
        """Mint a **system-admin password** credential (IAM2 bootstrap) — argon2id at
        rest, not tied to any tenant. No returned secret (the operator supplied it)."""
        self._validate_principal_id(principal_id)
        rec = CredentialRecord(
            id=secrets.token_hex(8),
            secret_hash=hash_password(password),
            tenant_id=SYSTEM_TENANT,
            principal_id=principal_id,
            roles=(SYSTEM_ROLE,),
            kind=HUMAN,
            secret_type=SECRET_PASSWORD,
            hash_algo=ALGO_ARGON2ID,
            created=_now_iso(),
            expires_at=expires_at,
            name=name,
        )
        records = self._load()
        records[rec.id] = rec
        self._save(records)
        return rec

    def has_system_admin(self) -> bool:
        """Whether any (active or not) system-admin credential exists — the bootstrap
        guard reads this so it can never silently clobber an established root of trust."""
        return any(
            r.tenant_id == SYSTEM_TENANT and SYSTEM_ROLE in r.roles for r in self._load().values()
        )

    # -- revoke / remove ------------------------------------------------------
    def revoke(self, credential_id: str) -> bool:
        """Revoke a credential by id (records ``revoked_at``). True if it transitioned."""
        records = self._load()
        rec = records.get(credential_id)
        if rec is None or rec.revoked_at is not None:
            return False
        from dataclasses import replace

        records[credential_id] = replace(rec, revoked_at=_now_iso())
        self._save(records)
        return True

    def remove_tenant(self, tenant_id: str) -> int:
        """Delete every credential belonging to ``tenant_id`` (lifecycle delete, T7)."""
        records = self._load()
        doomed = [cid for cid, r in records.items() if r.tenant_id == tenant_id]
        for cid in doomed:
            del records[cid]
        if doomed:
            self._save(records)
        return len(doomed)

    def revoke_for_principal(self, tenant_id: str, principal_id: str) -> int:
        """**Force-revoke** every credential for a principal (IAM8 deactivation). Returns
        the number newly revoked. The records are kept (revoked, not deleted) for audit."""
        from dataclasses import replace

        records = self._load()
        n = 0
        for cid, rec in records.items():
            if rec.tenant_id == tenant_id and rec.principal_id == principal_id and rec.revoked_at is None:
                records[cid] = replace(rec, revoked_at=_now_iso())
                n += 1
        if n:
            self._save(records)
        return n

    def set_roles(self, credential_id: str, roles) -> CredentialRecord:
        """Reassign a credential's roles (IAM8 role assignment). Validates each role."""
        from dataclasses import replace

        roles_t = tuple(validate_role(r) for r in roles)
        records = self._load()
        rec = records.get(credential_id)
        if rec is None:
            raise AuthError(f"no credential {credential_id!r} to update")
        updated = replace(rec, roles=roles_t)
        records[credential_id] = updated
        self._save(records)
        return updated

    def set_must_change_password(self, credential_id: str, value: bool) -> CredentialRecord:
        """Set/clear a credential's ``must_change_password`` flag (R2 sets it on the
        bootstrapped admin; R3 clears it on a successful password change)."""
        from dataclasses import replace

        records = self._load()
        rec = records.get(credential_id)
        if rec is None:
            raise AuthError(f"no credential {credential_id!r} to update")
        updated = replace(rec, must_change_password=bool(value))
        records[credential_id] = updated
        self._save(records)
        return updated

    def principals_for_tenant(self, tenant_id: str) -> list[dict]:
        """Distinct principals in a tenant, aggregated for user listing (IAM8): each
        ``{principal_id, roles, kind, active, credentials}`` — no secrets."""
        validate_tenant_id(tenant_id)
        by_principal: dict[str, dict] = {}
        for rec in self.list_for_tenant(tenant_id):
            u = by_principal.setdefault(
                rec.principal_id,
                {"principal_id": rec.principal_id, "roles": set(), "kind": rec.kind,
                 "active": False, "credentials": 0},
            )
            u["roles"].update(rec.roles)
            u["credentials"] += 1
            if rec.is_active():
                u["active"] = True
        out = []
        for u in sorted(by_principal.values(), key=lambda x: x["principal_id"]):
            u["roles"] = sorted(u["roles"])
            out.append(u)
        return out

    # -- lookup ---------------------------------------------------------------
    def get(self, credential_id: str) -> CredentialRecord | None:
        return self._load().get(credential_id)

    def list_for_tenant(self, tenant_id: str) -> list[CredentialRecord]:
        validate_tenant_id(tenant_id)
        return sorted(
            (r for r in self._load().values() if r.tenant_id == tenant_id),
            key=lambda r: r.created,
        )

    def validate(self, raw_token: str | None) -> CredentialRecord | None:
        """Return the active token :class:`CredentialRecord` for ``raw_token``, or None.

        Constant-time: hashes the presented token and compares against every stored
        **token** hash with :func:`hmac.compare_digest`. Password credentials (per-record
        salted argon2id) are not bearer-resolvable and are skipped here — use
        :meth:`verify_login`. An expired/revoked match returns None (fail closed)."""
        if not raw_token:
            return None
        presented = hash_token(raw_token)
        match: CredentialRecord | None = None
        for rec in self._load().values():
            if rec.secret_type != SECRET_TOKEN:
                continue
            if hmac.compare_digest(rec.secret_hash, presented):
                match = rec
        if match is None or not match.is_active():
            return None
        return match

    def verify_login(
        self, tenant_id: str, principal_id: str, password: str
    ) -> CredentialRecord | None:
        """Verify a ``(tenant, principal, password)`` login against a stored password
        credential (argon2id). Returns the active record on success, else None."""
        for rec in self._load().values():
            if (
                rec.secret_type == SECRET_PASSWORD
                and rec.tenant_id == tenant_id
                and rec.principal_id == principal_id
                and rec.is_active()
                and verify_password(rec.secret_hash, password)
            ):
                return rec
        return None

    def find_password_credential(
        self, tenant_id: str, principal_id: str
    ) -> CredentialRecord | None:
        """The (single) password credential for ``(tenant, principal)``, if any —
        regardless of active state (a reset may re-activate expiry). Used by the reset flow."""
        for rec in self._load().values():
            if (
                rec.secret_type == SECRET_PASSWORD
                and rec.tenant_id == tenant_id
                and rec.principal_id == principal_id
            ):
                return rec
        return None

    def set_password(self, credential_id: str, new_password: str) -> CredentialRecord:
        """Replace a password credential's secret (argon2id), in place. Used by the
        reset / change-password flows. The old hash is overwritten, never logged."""
        from dataclasses import replace

        records = self._load()
        rec = records.get(credential_id)
        if rec is None or rec.secret_type != SECRET_PASSWORD:
            raise AuthError(f"no password credential {credential_id!r} to update")
        updated = replace(rec, secret_hash=hash_password(new_password), hash_algo=ALGO_ARGON2ID)
        records[credential_id] = updated
        self._save(records)
        return updated

    def migrate(self) -> int:
        """Rewrite the store into the rich IAM1 schema, upgrading any legacy T3 rows.

        Access is already preserved on read (:meth:`CredentialRecord.from_dict`); this
        just persists the normalized form. Returns the number of rows that were in the
        legacy shape."""
        if not self.path.is_file():
            return 0
        try:
            data = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except (ValueError, OSError):
            return 0
        raw = data.get("credentials")
        if not isinstance(raw, dict):
            return 0
        legacy = sum(
            1 for rec in raw.values() if isinstance(rec, dict) and "token_hash" in rec and "secret_hash" not in rec
        )
        self._save(self._load())  # round-trips every row through from_dict/to_dict
        return legacy


# --- The resolver (the single boundary entry point) ------------------------


def _registry_for(data_root: Path | str | None) -> "tenancy.TenantRegistry":
    if data_root is None:
        return tenancy.TenantRegistry()
    return tenancy.TenantRegistry(Path(data_root) / config.REGISTRY_FILENAME)


def _resolve_tenant_record(
    credential: str | None, *, store: IdentityStore | None, data_root: Path | str | None
) -> CredentialRecord:
    """Shared fail-closed validation: a valid, active, **tenant** credential whose
    tenant is not suspended. Raises :class:`Deny` otherwise."""
    rec = (store or IdentityStore()).validate(credential)
    if rec is None:
        raise Deny("credential is absent, invalid, expired, or revoked", reason="invalid")
    # A system-admin credential is NOT a tenant principal — never resolve it as one.
    if rec.tenant_id == SYSTEM_TENANT or SYSTEM_ROLE in rec.roles:
        raise Deny("system-admin credential is not a tenant principal", reason="not_tenant")
    # A suspended tenant denies access while RETAINING its data (T7). Fail closed.
    tenant = _registry_for(data_root).get(rec.tenant_id)
    if tenant is not None and tenant.status != "active":
        raise Deny(f"tenant {rec.tenant_id!r} is {tenant.status} — access denied", reason="suspended")
    return rec


def resolve(
    credential: str | None,
    *,
    store: IdentityStore | None = None,
    data_root: Path | str | None = None,
) -> AuthenticatedPrincipal:
    """**The single resolver** every surface uses: an opaque bearer ``credential`` →
    :class:`AuthenticatedPrincipal`, or :class:`Deny` (fail closed).

    The tenant is taken **only** from the validated credential record; this function
    never reads a tenant id from anywhere else, so a client-supplied tenant id
    (header/body/path/content) is ignored by construction."""
    return _resolve_tenant_record(credential, store=store, data_root=data_root).authenticated()


def resolve_principal(
    credential: str | None,
    *,
    store: IdentityStore | None = None,
    data_root: Path | str | None = None,
) -> tuple[TenantContext, Principal]:
    """Resolve a credential to ``(TenantContext, Principal)`` for the surfaces that bind
    both (MCP/Web/CLI). Same fail-closed guarantees as :func:`resolve`; additionally
    opens the tenant (running the transparent default-tenant migration)."""
    rec = _resolve_tenant_record(credential, store=store, data_root=data_root)
    ctx = tenancy.open_tenant(rec.tenant_id, data_root=data_root)
    return ctx, rec.principal()


def resolve_admin(
    credential: str | None, *, store: IdentityStore | None = None
) -> Principal:
    """Resolve a credential to a **system-admin** :class:`Principal` (T7), or deny.

    A credential that is absent/invalid/expired/revoked — or that is a *tenant*
    credential rather than a system-admin one — raises :class:`Deny`."""
    rec = (store or IdentityStore()).validate(credential)
    if rec is None or rec.tenant_id != SYSTEM_TENANT or SYSTEM_ROLE not in rec.roles:
        raise Deny("not a valid system-admin credential", reason="not_admin")
    return rec.principal()


def is_system_admin(principal: "Principal | None") -> bool:
    return (
        isinstance(principal, Principal)
        and principal.tenant_id == SYSTEM_TENANT
        and principal.role == SYSTEM_ROLE
    )


# --- Active-principal binding (alongside the active tenant) -----------------

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
    store: IdentityStore | None = None,
    data_root: Path | str | None = None,
):
    """Resolve ``credential`` and bind BOTH the tenant and the principal for the block.
    Denies (raises :class:`Deny`) on an unresolved credential."""
    ctx, principal = resolve_principal(credential, store=store, data_root=data_root)
    with tenancy.use(ctx):
        token = bind_principal(principal)
        try:
            yield ctx, principal
        finally:
            unbind_principal(token)
