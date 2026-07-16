"""Session & token services (IAM3) — the runtime credential mechanics.

IAM1 gave us the identity model + the long-lived credential store; IAM2 the login
providers. IAM3 issues, validates, refreshes, and revokes the **runtime** credentials
the three surfaces actually carry, all resolving to the IAM1
:class:`~mnesis.identity.AuthenticatedPrincipal`:

  - **Web sessions** — opaque session tokens with **idle** (sliding) AND **absolute**
    (hard-cap) expiry, server-side validated so logout/revoke is *immediate*, and
    **rotated** on refresh (the old token is invalidated the instant a new one is minted).
    (Cookie/CSRF wiring is the Web prompt's job; this is the server-side token service.)
  - **Personal Access Tokens (PATs)** — user-issued, **named**, **scoped** to a subset
    of the issuer's permissions, expiring, revocable; shown once, stored hashed.
  - **Agent / machine API keys** — issued per agent-principal, **least-privilege
    scoped**, expiring, **rotatable**, revocable.

Every token is **opaque** and **hashed at rest** (``sha256(pepper||token)`` via the
IAM1 primitive), compared in **constant time**, and shown exactly once at creation.
Validation checks a dedicated **revocation store on every call**, so a logout / revoke
takes effect immediately (no waiting for expiry). **Scopes travel with the credential**
and are returned on the resolved principal for the PDP to enforce.

Both the token store and the revocation ledger live **outside any tenant root** (beside
the credential store) and are gitignored — server-side state, not derivable from Markdown.
"""

from __future__ import annotations

import hmac
import secrets
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from . import config, identity, tenancy
from .identity import (
    AGENT,
    AuthenticatedPrincipal,
    AuthError,
    Deny,
    HUMAN,
    MAINTAIN,
    Principal,
    READ,
    SYSTEM_TENANT,
    WRITE,
    hash_token,
    permissions_for,
)

# --- Token kinds + human-recognizable (still opaque) prefixes --------------

SESSION: str = "session"
PAT: str = "pat"
AGENT_KEY: str = "agent_key"
TOKEN_TYPES: frozenset[str] = frozenset({SESSION, PAT, AGENT_KEY})

#: Per-agent-kind **least-privilege** scopes for the agent-layer keys (IAM7). An agent
#: key carries the ``agent`` role; these scopes narrow it (effective = role ∩ scope):
#:   - writing agents ingest → ``write``;
#:   - action agents read Mnesis to compose (egress/send is separately controlled) → ``read``;
#:   - maintenance agents run the dream cycle → ``read`` + ``maintain``.
AGENT_KIND_SCOPES: dict[str, tuple[str, ...]] = {
    "writing": (WRITE,),
    "action": (READ,),
    "maintenance": (READ, MAINTAIN),
}

#: Prefixes aid humans/log-scrubbers spotting a leaked token by type; the secret is the
#: high-entropy remainder and the *whole* string is hashed, so the prefix grants nothing.
_PREFIX: dict[str, str] = {SESSION: "mns_sess_", PAT: "mns_pat_", AGENT_KEY: "mns_agt_"}


class TokenError(AuthError):
    """Base class for token-service faults."""


class ScopeError(TokenError, ValueError):
    """Requested scopes are not a subset of the issuer's permissions (least privilege)."""


def _now() -> float:
    return time.time()


def _mint_secret(token_type: str) -> str:
    return _PREFIX.get(token_type, "mns_") + secrets.token_urlsafe(32)


# --- Token record ----------------------------------------------------------


@dataclass(frozen=True)
class TokenRecord:
    """A stored runtime credential — **never the raw secret**, only its hash.

    ``id`` is a non-secret handle (for revocation/listing/audit and rotation lineage).
    ``absolute_expires_at`` is the hard deadline (rotation never extends it);
    ``idle_timeout`` (sessions only) is the sliding window measured from ``last_used_at``.
    """

    id: str
    token_hash: str
    token_type: str
    tenant_id: str
    principal_id: str
    roles: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()
    kind: str = HUMAN
    name: str | None = None
    created: str = ""
    absolute_expires_at: float | None = None  # epoch; None = no absolute expiry
    idle_timeout: int | None = None  # seconds; None = no idle expiry (PAT/agent key)
    last_used_at: float = 0.0
    revoked_at: str | None = None
    rotated_from: str | None = None  # the token this one replaced (refresh/rotate)
    rotated_to: str | None = None  # the token that replaced this one
    #: R3: a RESTRICTED session (the principal must change its password first). The PDP
    #: permits nothing but a change-own-password until this is cleared by rotating to a
    #: fresh full session on a successful change.
    must_change_password: bool = False

    # -- validity ------------------------------------------------------------
    def is_expired(self, now: float | None = None) -> bool:
        now = now if now is not None else _now()
        if self.absolute_expires_at is not None and now >= self.absolute_expires_at:
            return True
        if self.idle_timeout is not None and self.last_used_at:
            if now - self.last_used_at > self.idle_timeout:
                return True
        return False

    def authenticated(self) -> AuthenticatedPrincipal:
        return AuthenticatedPrincipal(
            tenant_id=self.tenant_id,
            principal_id=self.principal_id,
            roles=frozenset(self.roles),
            scopes=frozenset(self.scopes),
            kind=self.kind,
            must_change_password=self.must_change_password,
        )

    def public_dict(self) -> dict:
        """A safe view for listing/audit — excludes the token hash entirely."""
        d = {k: v for k, v in asdict(self).items() if k != "token_hash"}
        d["roles"] = list(self.roles)
        d["scopes"] = list(self.scopes)
        d["revoked"] = self.revoked_at is not None
        return d

    def to_dict(self) -> dict:
        d = asdict(self)
        d["roles"] = list(self.roles)
        d["scopes"] = list(self.scopes)
        return d

    @classmethod
    def from_dict(cls, d: dict, *, id_hint: str | None = None) -> "TokenRecord":
        return cls(
            id=d.get("id") or id_hint or "",
            token_hash=d.get("token_hash", ""),
            token_type=d.get("token_type", PAT),
            tenant_id=d["tenant_id"],
            principal_id=d["principal_id"],
            roles=tuple(d.get("roles") or ()),
            scopes=tuple(d.get("scopes") or ()),
            kind=d.get("kind", HUMAN),
            name=d.get("name"),
            created=d.get("created", ""),
            absolute_expires_at=d.get("absolute_expires_at"),
            idle_timeout=d.get("idle_timeout"),
            last_used_at=float(d.get("last_used_at") or 0.0),
            revoked_at=d.get("revoked_at"),
            rotated_from=d.get("rotated_from"),
            rotated_to=d.get("rotated_to"),
            must_change_password=bool(d.get("must_change_password", False)),
        )


# --- Revocation store (checked on EVERY validation) ------------------------


class RevocationStore:
    """The immediate-revocation ledger: a set of revoked token ids. Consulted on every
    :meth:`TokenService.validate`, so a logout / revoke / rotation denies the old token
    at once, independent of its expiry."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else config.revocations_path()

    def _load(self) -> set[str]:
        return set(config.load_json_object(self.path).get("revoked") or [])

    def _save(self, revoked: set[str]) -> None:
        config.atomic_write_json(self.path, {"revoked": sorted(revoked)})

    def add(self, token_id: str) -> None:
        revoked = self._load()
        if token_id not in revoked:
            revoked.add(token_id)
            self._save(revoked)

    def contains(self, token_id: str) -> bool:
        return token_id in self._load()


# --- The token service -----------------------------------------------------


class TokenService:
    """Issues / validates / refreshes / revokes the three runtime credential kinds."""

    def __init__(
        self,
        *,
        path: Path | str | None = None,
        revocations: RevocationStore | None = None,
    ) -> None:
        self.path = Path(path) if path is not None else config.tokens_path()
        self.revocations = revocations or RevocationStore(
            (Path(path).with_name(config.REVOCATIONS_FILENAME)) if path else None
        )

    # -- persistence ---------------------------------------------------------
    def _load(self) -> dict[str, TokenRecord]:
        raw = config.load_json_object(self.path).get("tokens")
        if not isinstance(raw, dict):
            return {}
        out: dict[str, TokenRecord] = {}
        for tid, rec in raw.items():
            try:
                out[tid] = TokenRecord.from_dict(rec, id_hint=tid)
            except (KeyError, TypeError):
                continue
        return out

    def _save(self, records: dict[str, TokenRecord]) -> None:
        payload = {"tokens": {tid: r.to_dict() for tid, r in records.items()}}
        config.atomic_write_json(self.path, payload)

    def _put(self, rec: TokenRecord) -> None:
        records = self._load()
        records[rec.id] = rec
        self._save(records)

    # -- issuance ------------------------------------------------------------
    def _issue(
        self,
        token_type: str,
        *,
        tenant_id: str,
        principal_id: str,
        roles: tuple[str, ...],
        scopes: tuple[str, ...],
        kind: str,
        name: str | None,
        absolute_expires_at: float | None,
        idle_timeout: int | None,
        rotated_from: str | None = None,
        must_change_password: bool = False,
        now: float | None = None,
    ) -> tuple[str, TokenRecord]:
        now = now if now is not None else _now()
        raw = _mint_secret(token_type)
        rec = TokenRecord(
            id=secrets.token_hex(8),
            token_hash=hash_token(raw),
            token_type=token_type,
            tenant_id=tenant_id,
            principal_id=principal_id,
            roles=tuple(roles),
            scopes=tuple(scopes),
            kind=kind,
            name=name,
            created=identity._now_iso(),
            absolute_expires_at=absolute_expires_at,
            idle_timeout=idle_timeout,
            last_used_at=now,
            rotated_from=rotated_from,
            must_change_password=must_change_password,
        )
        self._put(rec)
        return raw, rec

    def issue_session(
        self,
        principal: Principal | AuthenticatedPrincipal,
        *,
        idle_timeout: int | None = None,
        absolute_lifetime: int | None = None,
        scopes: tuple[str, ...] | list[str] | None = None,
        now: float | None = None,
    ) -> tuple[str, TokenRecord]:
        """Mint a web **session** for an already-authenticated principal (post-login).
        Idle timeout is sliding; the absolute lifetime is a hard cap rotation won't
        extend. Returns ``(raw_token, record)`` — the raw token is returned once."""
        now = now if now is not None else _now()
        idle = idle_timeout if idle_timeout is not None else config.MNESIS_SESSION_IDLE_SECONDS
        absolute = absolute_lifetime if absolute_lifetime is not None else config.MNESIS_SESSION_ABSOLUTE_SECONDS
        # A session represents the full logged-in user: it inherits the principal's own
        # scopes (empty = unrestricted within its roles), never a broadened set.
        sess_scopes = tuple(scopes) if scopes is not None else tuple(sorted(principal.scopes))
        # R3: a principal that must change its password gets a RESTRICTED session (the PDP
        # then permits nothing but a change-own-password until it rotates).
        return self._issue(
            SESSION,
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            roles=tuple(sorted(principal.roles)),
            scopes=sess_scopes,
            kind=getattr(principal, "kind", HUMAN),
            name=None,
            absolute_expires_at=(now + absolute) if absolute else None,
            idle_timeout=idle or None,
            must_change_password=bool(getattr(principal, "must_change_password", False)),
            now=now,
        )

    def issue_pat(
        self,
        principal: Principal | AuthenticatedPrincipal,
        name: str,
        scopes: tuple[str, ...] | list[str],
        *,
        ttl: int | None = None,
        now: float | None = None,
    ) -> tuple[str, TokenRecord]:
        """Mint a **PAT** for a user, named and **scoped to a subset of the issuer's
        permissions** (least privilege). Expires (default ``MNESIS_PAT_DEFAULT_TTL``),
        revocable, shown once."""
        if not name:
            raise TokenError("a PAT must be named")
        requested = tuple(scopes or ())
        self._require_subset(requested, principal)
        now = now if now is not None else _now()
        ttl = ttl if ttl is not None else config.MNESIS_PAT_DEFAULT_TTL
        return self._issue(
            PAT,
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            roles=tuple(sorted(principal.roles)),
            scopes=requested,
            kind=getattr(principal, "kind", HUMAN),
            name=name,
            absolute_expires_at=(now + ttl) if ttl else None,
            idle_timeout=None,
            now=now,
        )

    def issue_agent_key(
        self,
        tenant_id: str,
        principal_id: str,
        roles: tuple[str, ...] | list[str],
        scopes: tuple[str, ...] | list[str],
        *,
        name: str | None = None,
        ttl: int | None = None,
        now: float | None = None,
    ) -> tuple[str, TokenRecord]:
        """Mint an **agent/machine API key** for an agent-principal, **least-privilege
        scoped** (scopes must be a subset of the roles' permissions), expiring
        (default ``MNESIS_AGENT_KEY_DEFAULT_TTL``), rotatable, revocable."""
        roles_t = tuple(roles) or (AGENT,)
        allowed = permissions_for(roles_t)
        bad = [s for s in scopes if s not in allowed]
        if bad:
            raise ScopeError(f"agent-key scopes {bad} exceed the roles' permissions {sorted(allowed)}")
        now = now if now is not None else _now()
        ttl = ttl if ttl is not None else config.MNESIS_AGENT_KEY_DEFAULT_TTL
        return self._issue(
            AGENT_KEY,
            tenant_id=tenant_id,
            principal_id=principal_id,
            roles=roles_t,
            scopes=tuple(scopes or ()),
            kind=AGENT,
            name=name,
            absolute_expires_at=(now + ttl) if ttl else None,
            idle_timeout=None,
            now=now,
        )

    @staticmethod
    def _require_subset(requested, principal) -> None:
        """A PAT's scopes must be ⊆ the issuer's effective grant (role permissions plus
        any scopes the issuer itself already holds)."""
        allowed = set(permissions_for(principal.roles)) | set(getattr(principal, "scopes", ()) or ())
        bad = [s for s in requested if s not in allowed]
        if bad:
            raise ScopeError(f"requested scopes {bad} exceed the issuer's permissions {sorted(allowed)}")

    # -- validation ----------------------------------------------------------
    def validate(self, raw_token: str | None, *, now: float | None = None) -> AuthenticatedPrincipal:
        """Resolve an opaque token to an :class:`AuthenticatedPrincipal` (with scopes),
        or :class:`Deny`. Fail closed on absent/unknown/revoked/expired, and — resolving
        through IAM1 — on a suspended tenant. Constant-time hash comparison; the
        **revocation store is checked on every call**. On success the session's sliding
        idle window is advanced (``last_used_at``)."""
        now = now if now is not None else _now()
        if not raw_token:
            raise Deny("no token presented", reason="absent")
        presented = hash_token(raw_token)
        records = self._load()
        match: TokenRecord | None = None
        for rec in records.values():
            if hmac.compare_digest(rec.token_hash, presented):
                match = rec
        if match is None:
            raise Deny("unknown token", reason="unknown")
        # Immediate revocation — checked on every validation.
        if match.revoked_at is not None or self.revocations.contains(match.id):
            raise Deny("token has been revoked", reason="revoked")
        if match.is_expired(now):
            raise Deny("token has expired", reason="expired")
        # Resolve through IAM1: a suspended tenant denies access while retaining data.
        if match.tenant_id != SYSTEM_TENANT:
            tenant = tenancy.TenantRegistry().get(match.tenant_id) if _tenant_exists(match.tenant_id) else None
            if tenant is not None and tenant.status != "active":
                raise Deny(f"tenant {match.tenant_id!r} is {tenant.status} — access denied", reason="suspended")
        # Advance the sliding idle window for sessions (server-side, persisted).
        if match.idle_timeout is not None:
            records[match.id] = replace(match, last_used_at=now)
            self._save(records)
        return match.authenticated()

    # -- refresh / rotate ----------------------------------------------------
    def refresh_session(
        self, raw_token: str | None, *, now: float | None = None
    ) -> tuple[str, TokenRecord]:
        """Rotate a **session**: validate the presented token, mint a replacement (fresh
        idle window, the **same absolute deadline** — rotation never extends it), and
        **immediately invalidate the old one**. Returns ``(new_raw, new_record)``."""
        now = now if now is not None else _now()
        old = self._match_active(raw_token, now=now)
        if old.token_type != SESSION:
            raise TokenError("only sessions are refreshed; rotate PAT/agent keys instead")
        new_raw, new_rec = self._issue(
            SESSION,
            tenant_id=old.tenant_id,
            principal_id=old.principal_id,
            roles=old.roles,
            scopes=old.scopes,
            kind=old.kind,
            name=old.name,
            absolute_expires_at=old.absolute_expires_at,  # hard cap preserved
            idle_timeout=old.idle_timeout,
            rotated_from=old.id,
            # R3: a plain refresh preserves the restriction — it can NEVER clear it (only a
            # successful change-own-password mints a fresh FULL session).
            must_change_password=old.must_change_password,
            now=now,
        )
        self._invalidate(old.id, rotated_to=new_rec.id)
        return new_raw, new_rec

    def rotate(self, token_id: str, *, ttl: int | None = None, now: float | None = None) -> tuple[str, TokenRecord]:
        """Rotate a **PAT or agent key** by id: mint a replacement carrying the same
        identity/scopes/name with a fresh lifetime, and revoke the old one. Returns
        ``(new_raw, new_record)``."""
        now = now if now is not None else _now()
        old = self._load().get(token_id)
        if old is None:
            raise TokenError(f"unknown token id {token_id!r}")
        if old.token_type == SESSION:
            raise TokenError("use refresh_session to rotate a session")
        default_ttl = config.MNESIS_PAT_DEFAULT_TTL if old.token_type == PAT else config.MNESIS_AGENT_KEY_DEFAULT_TTL
        ttl = ttl if ttl is not None else default_ttl
        new_raw, new_rec = self._issue(
            old.token_type,
            tenant_id=old.tenant_id,
            principal_id=old.principal_id,
            roles=old.roles,
            scopes=old.scopes,
            kind=old.kind,
            name=old.name,
            absolute_expires_at=(now + ttl) if ttl else None,
            idle_timeout=old.idle_timeout,
            rotated_from=old.id,
            now=now,
        )
        self._invalidate(old.id, rotated_to=new_rec.id)
        return new_raw, new_rec

    # -- revoke --------------------------------------------------------------
    def revoke(self, token_id: str) -> bool:
        """Revoke a token by id — **immediately** (added to the revocation ledger and
        stamped ``revoked_at``). Idempotent. Denies on the very next validation."""
        records = self._load()
        rec = records.get(token_id)
        # Always record in the ledger (defends even if the record is gone).
        already = self.revocations.contains(token_id)
        self.revocations.add(token_id)
        if rec is not None and rec.revoked_at is None:
            records[token_id] = replace(rec, revoked_at=identity._now_iso())
            self._save(records)
            return True
        return not already

    def revoke_token(self, raw_token: str | None) -> bool:
        """Revoke by presenting the raw token (e.g. session logout). Best-effort match."""
        if not raw_token:
            return False
        presented = hash_token(raw_token)
        for rec in self._load().values():
            if hmac.compare_digest(rec.token_hash, presented):
                return self.revoke(rec.id)
        return False

    #: Logout is exactly an immediate session revoke.
    logout = revoke_token

    def revoke_all_for_principal(self, tenant_id: str, principal_id: str) -> int:
        """Revoke every token for a principal (e.g. on password reset / compromise)."""
        n = 0
        for rec in self._load().values():
            if rec.tenant_id == tenant_id and rec.principal_id == principal_id and rec.revoked_at is None:
                if self.revoke(rec.id):
                    n += 1
        return n

    # -- listing / lookup ----------------------------------------------------
    def get(self, token_id: str) -> TokenRecord | None:
        return self._load().get(token_id)

    def list_for_principal(self, tenant_id: str, principal_id: str) -> list[TokenRecord]:
        return sorted(
            (r for r in self._load().values()
             if r.tenant_id == tenant_id and r.principal_id == principal_id),
            key=lambda r: r.created,
        )

    # -- internals -----------------------------------------------------------
    def _match_active(self, raw_token: str | None, *, now: float) -> TokenRecord:
        """Return the live record for a raw token, or raise :class:`Deny` (same checks
        as :meth:`validate`, but returns the record for rotation)."""
        if not raw_token:
            raise Deny("no token presented", reason="absent")
        presented = hash_token(raw_token)
        for rec in self._load().values():
            if hmac.compare_digest(rec.token_hash, presented):
                if rec.revoked_at is not None or self.revocations.contains(rec.id):
                    raise Deny("token has been revoked", reason="revoked")
                if rec.is_expired(now):
                    raise Deny("token has expired", reason="expired")
                return rec
        raise Deny("unknown token", reason="unknown")

    def _invalidate(self, token_id: str, *, rotated_to: str) -> None:
        records = self._load()
        rec = records.get(token_id)
        if rec is not None:
            records[token_id] = replace(rec, revoked_at=identity._now_iso(), rotated_to=rotated_to)
            self._save(records)
        self.revocations.add(token_id)


def _tenant_exists(tenant_id: str) -> bool:
    try:
        return tenancy.TenantRegistry().exists(tenant_id)
    except Exception:
        return False


# --- Agent-layer least-privilege keys (IAM7) -------------------------------


def issue_agent_key_for(
    agent_kind: str,
    tenant_id: str,
    principal_id: str,
    *,
    name: str | None = None,
    ttl: int | None = None,
    service: "TokenService | None" = None,
) -> tuple[str, TokenRecord]:
    """Mint a **per-tenant, per-agent-principal** agent key with the documented
    least-privilege scopes for ``agent_kind`` (``writing`` / ``action`` / ``maintenance``
    — see :data:`AGENT_KIND_SCOPES`). Rotatable + revocable like any agent key."""
    scopes = AGENT_KIND_SCOPES.get(agent_kind)
    if scopes is None:
        raise TokenError(f"unknown agent kind {agent_kind!r}; one of {sorted(AGENT_KIND_SCOPES)}")
    return (service or TokenService()).issue_agent_key(
        tenant_id, principal_id, ("agent",), scopes, name=name or f"{agent_kind}-agent", ttl=ttl
    )


# --- The shared bearer resolver (used by MCP + the CLI) --------------------


def resolve_bearer(
    raw: str | None,
    *,
    token_store: "TokenService | None" = None,
    cred_store=None,
    data_root=None,
):
    """Resolve an opaque bearer credential to ``(TenantContext, Principal)`` using the
    **same** machinery every surface shares. Tries an IAM3 token/agent-key/PAT first
    (the token service, carrying its scopes), then falls back to a legacy IAM1
    credential (``mnesis auth issue``). Fail-closed: raises :class:`Deny` (with the token
    service's reason — ``expired``/``revoked``/``unknown``) or
    :class:`~mnesis.auth.InvalidCredential`; the tenant is taken only from the credential."""
    try:
        ap = (token_store or TokenService()).validate(raw)
        ctx = tenancy.open_tenant(ap.tenant_id, data_root=data_root)
        return ctx, ap.to_principal()
    except Deny as deny:
        from . import auth  # local import: auth → identity (no cycle with tokens)

        try:
            return auth.resolve_principal(raw, store=cred_store, data_root=data_root)
        except auth.AuthError:
            raise deny  # surface the token-service reason (expired/revoked/unknown)
