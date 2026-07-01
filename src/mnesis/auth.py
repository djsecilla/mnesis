"""Authentication — credentials → (TenantContext, Principal), resolved at boundaries.

**As of IAM1 this module is a thin compatibility facade over the unified identity
core (:mod:`mnesis.identity`).** The core is where the model lives — principals,
roles/permissions, scopes, the hashed credential store, and the single ``resolve``
resolver. This facade keeps the T3 import surface (``auth.Principal``,
``auth.Credential``, ``auth.CredentialStore``, ``auth.resolve_principal`` …) stable so
the existing surfaces (MCP, Web API, CLI, admin) need no change.

Everything here is re-exported from :mod:`mnesis.identity`. The load-bearing
guarantees are unchanged:

  - the tenant is taken **only** from the validated credential (never from a request
    header/body/path/content); an absent/invalid/expired/revoked credential is
    **denied** (fail closed, no default-tenant fallback);
  - secrets are **hashed at rest** and never stored or logged in the clear;
  - the credential store lives **outside any tenant root** (beside the tenant registry).

New code should prefer importing from :mod:`mnesis.identity` directly and using
:func:`identity.resolve` → :class:`identity.AuthenticatedPrincipal`.
"""

from __future__ import annotations

from .identity import (
    ADMIN,
    AGENT,
    # errors
    AuthError,
    # resolved-principal model (IAM1)
    AuthenticatedPrincipal,
    # credential store + record (T3 names kept as aliases below)
    CredentialRecord,
    Deny,
    HUMAN,
    IdentityStore,
    InvalidCredential,
    InvalidRole,
    MAINTAIN,
    # permissions
    PERMISSIONS,
    # models
    Principal,
    READ,
    # role model
    Role,
    ROLES,
    # secret hashing
    SECRET_PASSWORD,
    SECRET_TOKEN,
    # system-admin boundary
    SYSTEM_ROLE,
    SYSTEM_TENANT,
    User,
    WRITE,
    authenticated,
    bind_principal,
    current_principal,
    current_principal_or_none,
    hash_password,
    hash_token,
    is_system_admin,
    permissions_for,
    # the single resolver + the surface adapters
    resolve,
    resolve_admin,
    resolve_principal,
    unbind_principal,
    validate_role,
    verify_password,
)

#: T3 compatibility aliases — the identity core renamed these, the old names endure.
Credential = CredentialRecord
CredentialStore = IdentityStore

__all__ = [
    "ADMIN",
    "AGENT",
    "AuthError",
    "AuthenticatedPrincipal",
    "Credential",
    "CredentialRecord",
    "CredentialStore",
    "Deny",
    "HUMAN",
    "IdentityStore",
    "InvalidCredential",
    "InvalidRole",
    "MAINTAIN",
    "PERMISSIONS",
    "Principal",
    "READ",
    "ROLES",
    "Role",
    "SECRET_PASSWORD",
    "SECRET_TOKEN",
    "SYSTEM_ROLE",
    "SYSTEM_TENANT",
    "User",
    "WRITE",
    "authenticated",
    "bind_principal",
    "current_principal",
    "current_principal_or_none",
    "hash_password",
    "hash_token",
    "is_system_admin",
    "permissions_for",
    "resolve",
    "resolve_admin",
    "resolve_principal",
    "unbind_principal",
    "validate_role",
    "verify_password",
]
