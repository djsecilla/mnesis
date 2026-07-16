"""Self-service account operations — the surface-neutral change-own-password (R3).

A principal may **always** change its OWN password (never its role). This is the one
action a RESTRICTED (must_change_password) session is permitted, enforced centrally by the
PDP (:data:`mnesis.authz.PASSWORD_CHANGE`). This module orchestrates the full change so
Web, CLI, and MCP share one implementation:

  verify current (argon2id) → policy + no-reuse → set new + clear ``must_change_password``
  → **rotate the session** (mint a fresh FULL session, invalidate the restricted one)
  → rate-limited + audited (never a secret).

The credential-change + rate-limit + audit live in :meth:`LocalPasswordProvider.change_password`;
this adds the session rotation so a successful change immediately restores normal access.
"""

from __future__ import annotations

from . import identity, providers, tokens


def change_own_password(
    tenant_id: str,
    principal_id: str,
    current_password: str,
    new_password: str,
    *,
    session_token: str | None = None,
    client_ip: str | None = None,
    provider: "providers.LocalPasswordProvider | None" = None,
    token_service: "tokens.TokenService | None" = None,
) -> dict:
    """Change ``principal_id``'s own password and (if a ``session_token`` is supplied)
    **rotate** its session: mint a fresh **full** session and revoke the restricted one.

    Raises the provider's errors on failure — :class:`~mnesis.providers.AuthenticationFailed`
    (wrong current password, rate-limited failure), :class:`~mnesis.providers.AccountLocked`
    (throttled), :class:`~mnesis.providers.PasswordPolicyError` (weak / reused). On success
    the ``must_change_password`` flag is cleared and the returned principal is unrestricted.

    Returns ``{principal, credential_id, rotated, new_session}`` — ``new_session`` is the raw
    replacement session token (returned once) when a ``session_token`` was rotated, else
    ``None``. **Never** contains a password.
    """
    prov = provider or providers.LocalPasswordProvider()
    svc = token_service or tokens.TokenService()

    # Verify + policy + no-reuse + set + clear must_change_password (rate-limited, audited).
    rec = prov.change_password(
        tenant_id, principal_id, current_password, new_password, client_ip=client_ip
    )
    fresh: identity.Principal = rec.principal()  # must_change_password now False

    new_session: str | None = None
    rotated = False
    if session_token:
        # Invalidate the RESTRICTED session first, then mint a fresh FULL one (unrestricted
        # — issue_session reads the cleared flag off `fresh`). Old token denies immediately.
        svc.logout(session_token)
        new_session, _ = svc.issue_session(fresh)
        rotated = True

    return {
        "principal": fresh,
        "credential_id": rec.id,
        "rotated": rotated,
        "new_session": new_session,
    }
