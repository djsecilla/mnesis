"""Auth audit (IAM8) — one append-only record of *who did what, when, and the result*.

This centralises authentication/authorization auditing on top of the IAM2
:class:`~mnesis.providers.AuthAuditLog` (``DATA_ROOT/auth_audit.jsonl``, outside every
tenant root). It records the events the security drills care about — **logins**
(success/failure), **token/credential issue / rotate / revoke**, **user lifecycle**, and
**PDP denials** — each tagged with the ``principal``, ``tenant``, ``credential id``,
``action``, and ``result``. It **never records a secret** (no password, token, or hash);
every event is tagged with its tenant so a per-tenant view is a filter, and admin/system
actions also land in the system audit log (``admin.SystemAuditLog``).

Wiring: :func:`enable_pdp_audit` registers a sink so the single PDP (`authz`) reports every
deny here — the surfaces call it once at their boundary (CLI ``main``; the HTTP server).
"""

from __future__ import annotations

from . import authz, providers


def record(
    event: str,
    *,
    tenant_id: str | None = None,
    principal_id: str | None = None,
    credential_id: str | None = None,
    action: str | None = None,
    result: str | None = None,
    reason: str | None = None,
    log: providers.AuthAuditLog | None = None,
    **detail,
) -> dict:
    """Append one auth-audit record. Never pass a secret — ids/actions/results only."""
    return (log or providers.AuthAuditLog()).record(
        event,
        tenant_id=tenant_id,
        principal_id=principal_id,
        reason=reason,
        credential_id=credential_id,
        action=action,
        result=result,
        **detail,
    )


def _pdp_sink(decision) -> None:
    """authz audit sink: every PDP deny becomes an audit record (no values)."""
    record(
        "pdp_deny",
        tenant_id=decision.tenant_id,
        principal_id=decision.principal_id,
        action=decision.action,
        result="deny",
        reason=decision.reason,
    )


def enable_pdp_audit() -> None:
    """Route every PDP denial to the auth audit log (called at a surface boundary)."""
    authz.set_audit_sink(_pdp_sink)


def disable_pdp_audit() -> None:
    authz.set_audit_sink(None)
