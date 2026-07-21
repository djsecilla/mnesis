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

#: The user-lifecycle events the admin "recent activity" view surfaces (R9).
USER_LIFECYCLE_EVENTS: frozenset[str] = frozenset({
    "user_created", "user_role_assigned", "user_deactivated", "user_reactivated",
    "user_password_reset", "user_credentials_revoked", "user_deleted",
})

#: The only fields exposed to that view — ids/actions/results/timestamps, **never** a secret
#: (the audit holds none anyway). Anything else in a record is dropped.
_SAFE_AUDIT_FIELDS: tuple[str, ...] = (
    "ts", "event", "actor", "principal_id", "tenant_id", "action", "result", "role",
)


def recent_user_events(
    actor_principal_id: str, *, limit: int = 20, log: providers.AuthAuditLog | None = None
) -> list[dict]:
    """Recent **user-management** audit events performed BY ``actor_principal_id`` — the
    read-only feed for the admin Users screen (R9). Scoped to the actor's own actions
    (so it never leaks another admin's/tenant's activity), newest first, and reduced to
    the non-secret :data:`_SAFE_AUDIT_FIELDS`. Best-effort: a missing/unreadable log yields
    an empty list (surfacing activity must never break the screen)."""
    try:
        rows = (log or providers.AuthAuditLog()).all()
    except Exception:  # noqa: BLE001 — a read-only convenience view never raises
        return []
    events = [
        {k: r[k] for k in _SAFE_AUDIT_FIELDS if k in r}
        for r in rows
        if r.get("event") in USER_LIFECYCLE_EVENTS and r.get("actor") == actor_principal_id
    ]
    events.reverse()  # newest first
    return events[: max(0, int(limit))]


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
