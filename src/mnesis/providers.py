"""Identity providers (IAM2) — the pluggable "who are you?" backend.

IAM1 gave us the identity *model* and credential store; IAM2 adds the **login**
backend behind a seam so the default local username/password provider can later be
swapped for (or joined by) an enterprise IdP (OIDC/SAML) **without changing anything
downstream** — every provider resolves to the very same :class:`~mnesis.identity.Principal`.

What ships here:

  - :class:`IdentityProvider` — the seam. ``authenticate(...) -> Principal`` or a
    fail (raises :class:`AuthenticationFailed` / :class:`AccountLocked`).
  - :class:`LocalPasswordProvider` — the default: **argon2id** password hashing (via
    the identity core), a password policy, verification, a token-based **password-reset**
    flow (single-use, expiring), and change-password.
  - :class:`OIDCProvider` — a documented **seam stub** that satisfies the interface but
    is not wired to a real IdP (it fails closed until configured).
  - Brute-force protection: :class:`ThrottleStore` (per-account **and** per-IP
    backoff/lockout) and an append-only :class:`AuthAuditLog` of auth events.
  - :class:`ResetTokenStore` — hashed, expiring, single-use reset tokens.

Security posture: passwords are **argon2id** at rest and **never logged**; reset tokens
are hashed, single-use, and expiring; failures are throttled and audited; there are **no
default/hardcoded credentials** anywhere (bootstrap requires operator input — see
:func:`mnesis.admin.bootstrap_system_admin`). All auxiliary state lives OUTSIDE any
tenant root, beside the credential store.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from . import config, identity
from .identity import (
    ALGO_ARGON2ID,
    AuthError,
    Principal,
    SECRET_PASSWORD,
    hash_token,
    hash_password,
    validate_role,
    verify_password,
)

log = logging.getLogger("mnesis.providers")


# --- Errors ----------------------------------------------------------------


class AuthenticationError(AuthError):
    """Base class for a provider login failure."""


class AuthenticationFailed(AuthenticationError):
    """Bad credentials (unknown principal or wrong secret). Deliberately generic — the
    message never distinguishes "no such user" from "wrong password" (no enumeration)."""


class AccountLocked(AuthenticationError):
    """Too many recent failures for this account or IP; try again after ``retry_after``."""

    def __init__(self, message: str, *, retry_after: float = 0.0) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class PasswordPolicyError(AuthError, ValueError):
    """A supplied password fails the policy (too short / weak)."""


class ResetTokenError(AuthError):
    """A password-reset token is missing, wrong, expired, or already used."""


# --- Password policy -------------------------------------------------------

#: A tiny denylist of obviously-weak secrets. Not a substitute for length — argon2id
#: plus a real minimum length is the strength; this only rejects the worst offenders.
_WEAK_PASSWORDS: frozenset[str] = frozenset(
    {"password", "passw0rd", "changeme", "letmein", "admin", "welcome", "qwerty"}
)


def check_password_policy(password: str) -> str:
    """Return ``password`` if it satisfies the policy, else raise
    :class:`PasswordPolicyError`. Policy: non-empty, at least
    ``MNESIS_PASSWORD_MIN_LENGTH`` chars, not all whitespace, not a known-weak value.
    The password itself is never included in the error message (never logged)."""
    if not password or not password.strip():
        raise PasswordPolicyError("password must not be empty")
    if len(password) < config.MNESIS_PASSWORD_MIN_LENGTH:
        raise PasswordPolicyError(
            f"password too short (minimum {config.MNESIS_PASSWORD_MIN_LENGTH} characters)"
        )
    if password.strip().lower() in _WEAK_PASSWORDS:
        raise PasswordPolicyError("password is too common / easily guessed")
    return password


# --- Auth audit (append-only; never records a secret) ----------------------


class AuthAuditLog:
    """Append-only JSONL of authentication events — OUTSIDE any tenant root. Records
    who/when/where and the outcome; **never a password or a token**."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else config.auth_audit_path()

    def record(
        self,
        event: str,
        *,
        tenant_id: str | None = None,
        principal_id: str | None = None,
        client_ip: str | None = None,
        provider: str | None = None,
        reason: str | None = None,
        **detail,
    ) -> dict:
        rec = {
            "ts": identity._now_iso(),
            "event": event,
            "tenant_id": tenant_id,
            "principal_id": principal_id,
            "client_ip": client_ip,
            "provider": provider,
            "reason": reason,
            **detail,
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


# --- Brute-force throttle (per-account AND per-IP) -------------------------


@dataclass(frozen=True)
class ThrottleStatus:
    locked: bool
    retry_after: float  # seconds until the lock lifts (0 when not locked)
    failures: int


class ThrottleStore:
    """A small JSON ledger of recent failures per key (``acct:<tenant>:<principal>``
    and ``ip:<addr>``). After :data:`MNESIS_AUTH_MAX_FAILURES` failures inside
    :data:`MNESIS_AUTH_FAILURE_WINDOW` the key locks for
    :data:`MNESIS_AUTH_LOCKOUT_SECONDS`, doubling on each further failure up to
    :data:`MNESIS_AUTH_LOCKOUT_MAX_SECONDS` (exponential backoff). Success clears it.

    Both dimensions are checked before a login and incremented on failure, so an
    attacker is throttled whether they spread guesses across accounts (per-IP) or
    hammer one account from many IPs (per-account)."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else config.throttle_path()

    def _load(self) -> dict[str, dict]:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except (ValueError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    @staticmethod
    def account_key(tenant_id: str, principal_id: str) -> str:
        return f"acct:{tenant_id}:{principal_id}"

    @staticmethod
    def ip_key(client_ip: str) -> str:
        return f"ip:{client_ip}"

    def _status_for(self, rec: dict | None, now: float) -> ThrottleStatus:
        if not rec:
            return ThrottleStatus(False, 0.0, 0)
        locked_until = float(rec.get("locked_until") or 0.0)
        if locked_until > now:
            return ThrottleStatus(True, locked_until - now, int(rec.get("failures", 0)))
        return ThrottleStatus(False, 0.0, int(rec.get("failures", 0)))

    def status(self, key: str, *, now: float | None = None) -> ThrottleStatus:
        now = now if now is not None else time.time()
        return self._status_for(self._load().get(key), now)

    def check(self, keys: list[str], *, now: float | None = None) -> ThrottleStatus:
        """The most-restrictive status across ``keys`` (locked if any is locked)."""
        now = now if now is not None else time.time()
        data = self._load()
        worst = ThrottleStatus(False, 0.0, 0)
        for key in keys:
            st = self._status_for(data.get(key), now)
            if st.locked and st.retry_after > worst.retry_after:
                worst = st
        return worst

    def record_failure(self, key: str, *, now: float | None = None) -> ThrottleStatus:
        now = now if now is not None else time.time()
        window = config.MNESIS_AUTH_FAILURE_WINDOW
        maxf = config.MNESIS_AUTH_MAX_FAILURES
        base = config.MNESIS_AUTH_LOCKOUT_SECONDS
        cap = config.MNESIS_AUTH_LOCKOUT_MAX_SECONDS
        data = self._load()
        rec = data.get(key) or {}
        first = float(rec.get("first_failure") or now)
        failures = int(rec.get("failures", 0))
        # Reset the counter if the window elapsed since the first failure.
        if now - first > window:
            first, failures = now, 0
        failures += 1
        locked_until = 0.0
        if maxf > 0 and failures >= maxf:
            over = failures - maxf
            locked_until = now + min(cap, base * (2 ** over))
        data[key] = {
            "failures": failures,
            "first_failure": first,
            "last_failure": now,
            "locked_until": locked_until,
        }
        self._save(data)
        return self._status_for(data[key], now)

    def clear(self, key: str) -> None:
        data = self._load()
        if key in data:
            del data[key]
            self._save(data)


# --- Reset tokens (hashed, expiring, single-use) ---------------------------


class ResetTokenStore:
    """Password-reset tokens: one active token per ``(tenant, principal)``, hashed at
    rest (``sha256(pepper||token)``), expiring, and **single-use**. Issuing a new token
    invalidates any previous one; consuming a valid token marks it used."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else config.reset_tokens_path()

    def _load(self) -> dict[str, dict]:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except (ValueError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    @staticmethod
    def _key(tenant_id: str, principal_id: str) -> str:
        return f"{tenant_id}:{principal_id}"

    def issue(
        self, tenant_id: str, principal_id: str, *, ttl: int | None = None, now: float | None = None
    ) -> str:
        """Mint a reset token, returning the raw value **once** (only its hash is stored)."""
        now = now if now is not None else time.time()
        ttl = ttl if ttl is not None else config.MNESIS_RESET_TOKEN_TTL
        raw = secrets.token_urlsafe(32)
        data = self._load()
        data[self._key(tenant_id, principal_id)] = {
            "token_hash": hash_token(raw),
            "expires_at": now + ttl,
            "used": False,
            "created": identity._now_iso(),
        }
        self._save(data)
        return raw

    def consume(
        self, tenant_id: str, principal_id: str, raw_token: str, *, now: float | None = None
    ) -> bool:
        """Validate + spend a reset token. True only if it matches, is unexpired, and was
        unused; marks it used (single-use). Constant-time hash comparison."""
        now = now if now is not None else time.time()
        data = self._load()
        rec = data.get(self._key(tenant_id, principal_id))
        if not rec or rec.get("used"):
            return False
        if float(rec.get("expires_at") or 0.0) <= now:
            return False
        if not raw_token or not hmac.compare_digest(rec.get("token_hash", ""), hash_token(raw_token)):
            return False
        rec["used"] = True
        rec["used_at"] = identity._now_iso()
        data[self._key(tenant_id, principal_id)] = rec
        self._save(data)
        return True


# --- The provider seam -----------------------------------------------------


class IdentityProvider(ABC):
    """The "who are you?" seam. Every provider — local password, OIDC, SAML — maps a
    login attempt to the **same** :class:`~mnesis.identity.Principal`, so nothing
    downstream (the PDP, the surfaces) depends on how identity was proven."""

    #: A short, stable provider key (matches ``MNESIS_IDENTITY_PROVIDER``).
    name: str = "abstract"

    @abstractmethod
    def authenticate(
        self,
        tenant_id: str,
        principal_id: str,
        secret: str,
        *,
        client_ip: str | None = None,
    ) -> Principal:
        """Prove identity and return the resolved :class:`Principal`, or raise an
        :class:`AuthenticationError` (fail closed). ``secret`` is the password for the
        local provider, or the code/assertion for a federated one."""
        raise NotImplementedError


class LocalPasswordProvider(IdentityProvider):
    """The default provider: username/password against the local credential store, with
    argon2id verification, brute-force throttling, auditing, and a reset flow."""

    name = "local"

    def __init__(
        self,
        *,
        store: identity.IdentityStore | None = None,
        throttle: ThrottleStore | None = None,
        audit: AuthAuditLog | None = None,
        resets: ResetTokenStore | None = None,
    ) -> None:
        self.store = store or identity.IdentityStore()
        self.throttle = throttle or ThrottleStore()
        self.audit = audit or AuthAuditLog()
        self.resets = resets or ResetTokenStore()

    # -- registration -------------------------------------------------------
    def register(
        self,
        tenant_id: str,
        principal_id: str,
        role: str,
        password: str,
        *,
        name: str | None = None,
        scopes=None,
        must_change_password: bool = False,
    ) -> identity.CredentialRecord:
        """Create a password credential (policy-checked, argon2id at rest).
        ``must_change_password`` flags a credential that must be rotated on first use
        (the bootstrapped initial admin; R2/R3)."""
        validate_role(role)
        check_password_policy(password)
        return self.store.issue_password(
            tenant_id, principal_id, role, password, name=name, scopes=scopes,
            must_change_password=must_change_password,
        )

    # -- authentication -----------------------------------------------------
    def authenticate(
        self,
        tenant_id: str,
        principal_id: str,
        secret: str,
        *,
        client_ip: str | None = None,
    ) -> Principal:
        now = time.time()
        acct = ThrottleStore.account_key(tenant_id, principal_id)
        keys = [acct] + ([ThrottleStore.ip_key(client_ip)] if client_ip else [])

        # 1) Refuse up-front if the account or the IP is currently locked.
        locked = self.throttle.check(keys, now=now)
        if locked.locked:
            self.audit.record(
                "auth_locked", tenant_id=tenant_id, principal_id=principal_id,
                client_ip=client_ip, provider=self.name, reason="throttled",
            )
            raise AccountLocked(
                "too many failed attempts; try again later", retry_after=locked.retry_after
            )

        # 2) Verify the password (argon2id) — constant work whether or not it matches.
        rec = self.store.verify_login(tenant_id, principal_id, secret)
        if rec is None:
            for key in keys:
                self.throttle.record_failure(key, now=now)
            self.audit.record(
                "auth_failure", tenant_id=tenant_id, principal_id=principal_id,
                client_ip=client_ip, provider=self.name, reason="bad_credentials",
            )
            raise AuthenticationFailed("invalid credentials")

        # 3) Success — clear the throttle for these keys and audit.
        for key in keys:
            self.throttle.clear(key)
        self.audit.record(
            "auth_success", tenant_id=tenant_id, principal_id=principal_id,
            client_ip=client_ip, provider=self.name,
        )
        return rec.principal()

    # -- password reset (token-based, single-use, expiring) -----------------
    def begin_reset(
        self, tenant_id: str, principal_id: str, *, ttl: int | None = None
    ) -> str | None:
        """Start a reset: mint a single-use, expiring token (returned once) if the
        principal has a password credential. Returns ``None`` (no token, no error) when
        there is no such account — the caller reveals nothing either way (no enumeration)."""
        if self.store.find_password_credential(tenant_id, principal_id) is None:
            self.audit.record(
                "reset_requested", tenant_id=tenant_id, principal_id=principal_id,
                provider=self.name, reason="no_account",
            )
            return None
        token = self.resets.issue(tenant_id, principal_id, ttl=ttl)
        self.audit.record(
            "reset_requested", tenant_id=tenant_id, principal_id=principal_id, provider=self.name
        )
        return token

    def reset_password(
        self, tenant_id: str, principal_id: str, reset_token: str, new_password: str
    ) -> identity.CredentialRecord:
        """Spend a reset token and set a new password. The token is **single-use** and
        must be unexpired; the new password is policy-checked and argon2id-hashed."""
        check_password_policy(new_password)
        rec = self.store.find_password_credential(tenant_id, principal_id)
        if rec is None:
            raise ResetTokenError("no password credential to reset")
        if not self.resets.consume(tenant_id, principal_id, reset_token):
            self.audit.record(
                "reset_failed", tenant_id=tenant_id, principal_id=principal_id,
                provider=self.name, reason="bad_or_used_token",
            )
            raise ResetTokenError("reset token is missing, wrong, expired, or already used")
        updated = self.store.set_password(rec.id, new_password)
        # A successful reset also clears any standing lockout for the account.
        self.throttle.clear(ThrottleStore.account_key(tenant_id, principal_id))
        self.audit.record(
            "reset_completed", tenant_id=tenant_id, principal_id=principal_id, provider=self.name
        )
        return updated

    def change_password(
        self,
        tenant_id: str,
        principal_id: str,
        old_password: str,
        new_password: str,
        *,
        client_ip: str | None = None,
        now: float | None = None,
    ) -> identity.CredentialRecord:
        """Change a password given the current one (no reset token needed). Verifies the
        current password (argon2id), enforces the password **policy**, **forbids reuse**
        of the same password, and — R3 — **clears ``must_change_password``**. Attempts are
        **rate-limited** (per-account + per-IP, same throttle as login) and **audited**
        (never a secret). Returns the updated record."""
        acct = ThrottleStore.account_key(tenant_id, principal_id)
        keys = [acct] + ([ThrottleStore.ip_key(client_ip)] if client_ip else [])

        # Refuse up-front if throttled (repeated wrong-current-password attempts lock out).
        locked = self.throttle.check(keys, now=now)
        if locked.locked:
            self.audit.record(
                "password_change_locked", tenant_id=tenant_id, principal_id=principal_id,
                client_ip=client_ip, provider=self.name, reason="throttled",
            )
            raise AccountLocked("too many attempts; try again later", retry_after=locked.retry_after)

        # Verify the current password. A wrong one is a throttled failure (like a login).
        rec = self.store.verify_login(tenant_id, principal_id, old_password)
        if rec is None:
            for key in keys:
                self.throttle.record_failure(key, now=now)
            self.audit.record(
                "password_change_failed", tenant_id=tenant_id, principal_id=principal_id,
                client_ip=client_ip, provider=self.name, reason="bad_current_password",
            )
            raise AuthenticationFailed("current password is incorrect")

        check_password_policy(new_password)  # a weak new password is refused (not a guess)
        if new_password == old_password:      # forbid reuse of the same password
            self.audit.record(
                "password_change_failed", tenant_id=tenant_id, principal_id=principal_id,
                client_ip=client_ip, provider=self.name, reason="reuse",
            )
            raise PasswordPolicyError("the new password must differ from the current one")

        updated = self.store.set_password(rec.id, new_password)
        updated = self.store.set_must_change_password(updated.id, False)  # R3: lift the restriction
        for key in keys:
            self.throttle.clear(key)
        self.audit.record(
            "password_changed", tenant_id=tenant_id, principal_id=principal_id,
            client_ip=client_ip, provider=self.name,
        )
        return updated


class OIDCProvider(IdentityProvider):
    """A **seam stub** for an OpenID Connect IdP — present to prove the interface is
    provider-agnostic, **not** a working integration. It fails closed until a real
    integration is supplied (token validation + claims → ``Principal`` mapping).

    A concrete implementation would: validate the OIDC ID token / authorization code
    against the configured issuer, map the verified claims (subject, tenant, groups) to
    a :class:`Principal` with roles/scopes, and return it — resolving to the *same*
    Principal type as the local provider, so nothing downstream changes."""

    name = "oidc"

    def __init__(self, *, issuer: str | None = None, client_id: str | None = None) -> None:
        self.issuer = issuer
        self.client_id = client_id

    def authenticate(
        self,
        tenant_id: str,
        principal_id: str,
        secret: str,
        *,
        client_ip: str | None = None,
    ) -> Principal:
        raise AuthenticationError(
            "OIDC provider is a seam stub and not configured; use the local password "
            "provider or supply a concrete OIDC integration"
        )


# --- Provider selection ----------------------------------------------------

_PROVIDERS: dict[str, type[IdentityProvider]] = {
    "local": LocalPasswordProvider,
    "oidc": OIDCProvider,
}


def get_identity_provider(name: str | None = None, **kwargs) -> IdentityProvider:
    """Construct the configured identity provider (default ``MNESIS_IDENTITY_PROVIDER``).
    The returned provider always resolves to a :class:`~mnesis.identity.Principal`."""
    key = (name or config.MNESIS_IDENTITY_PROVIDER or "local").strip().lower()
    cls = _PROVIDERS.get(key)
    if cls is None:
        raise AuthError(f"unknown identity provider {key!r}; one of {sorted(_PROVIDERS)}")
    return cls(**kwargs)
