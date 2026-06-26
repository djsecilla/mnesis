"""Per-tenant agent runtime (T6).

Each agent runs **confined to one tenant**: it reaches Mnesis only through that
tenant's MCP credential (which resolves server-side to that tenant + an agent
principal — T3/T5), and **all** of its agent-side governance state lives under that
tenant's own directories. No scope shares a store, ledger, registry, credential, or
egress config with another tenant.

A :class:`TenantScope` is the single per-tenant handle. It carries the tenant's
credential + MCP url and resolves every governance path:

    STATE_BASE/tenants/<tenant_id>/
      runs/                # the run-audit JSONL + dream/action proposals + reports
      runs/connectors/     # the writing processed-state ledger + dead-letter
      egress/              # the egress quota ledger + email at-most-once + send-audit
      checkpoints.db       # the LangGraph checkpointer

plus a per-tenant notes inbox, action outbox, and egress config (allowlists +
quotas + kill-switch). Resolution is **fail-closed**: a tenant with no credential
raises :class:`UnresolvedTenant` and its agents do not start.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from . import config


class UnresolvedTenant(Exception):
    """A tenant could not be resolved to a usable scope (no credential) — fail closed."""


def _state_base() -> Path:
    """The base directory under which every tenant's agent state is partitioned."""
    raw = os.environ.get("MNESIS_AGENTS_STATE_BASE")
    return Path(raw).expanduser() if raw else config.MNESIS_AGENTS_AUDIT_DIR


# --- Per-tenant egress config ----------------------------------------------


@dataclass(frozen=True)
class EgressSettings:
    """A tenant's own external-send policy — allowlists, quotas, switches. Default-
    deny, exactly like the global plane, but **partitioned per tenant**."""

    enabled: bool = False
    kill: bool = False
    email_enabled: bool = False
    dryrun: bool = True
    recipient_allowlist: frozenset[str] = field(default_factory=frozenset)
    endpoint_allowlist: frozenset[str] = field(default_factory=frozenset)
    rate_limit: int = 10
    rate_window_seconds: float = 3600.0
    daily_quota: int = 50
    global_rate_limit: int = 30
    global_daily_quota: int = 200

    @classmethod
    def from_config(cls) -> "EgressSettings":
        from .egress import _parse_list

        return cls(
            enabled=config.MNESIS_EGRESS_ENABLED,
            kill=config.MNESIS_EGRESS_KILL,
            email_enabled=config.MNESIS_EMAIL_ENABLED,
            dryrun=config.MNESIS_EMAIL_DRYRUN,
            recipient_allowlist=_parse_list(config.MNESIS_EGRESS_RECIPIENT_ALLOWLIST),
            endpoint_allowlist=_parse_list(config.MNESIS_EGRESS_ENDPOINT_ALLOWLIST),
            rate_limit=config.MNESIS_EGRESS_RATE_LIMIT,
            rate_window_seconds=config.MNESIS_EGRESS_RATE_WINDOW_SECONDS,
            daily_quota=config.MNESIS_EGRESS_DAILY_QUOTA,
            global_rate_limit=config.MNESIS_EGRESS_GLOBAL_RATE_LIMIT,
            global_daily_quota=config.MNESIS_EGRESS_GLOBAL_DAILY_QUOTA,
        )

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "EgressSettings":
        base = cls.from_config()
        if not d:
            return base
        def _set(v):  # allowlists may be a list or comma string
            if isinstance(v, str):
                return frozenset(e.strip().lower() for e in v.split(",") if e.strip())
            return frozenset(str(e).strip().lower() for e in (v or []))
        return replace(
            base,
            enabled=bool(d.get("enabled", base.enabled)),
            kill=bool(d.get("kill", base.kill)),
            email_enabled=bool(d.get("email_enabled", base.email_enabled)),
            dryrun=bool(d.get("dryrun", base.dryrun)),
            recipient_allowlist=_set(d["recipient_allowlist"]) if "recipient_allowlist" in d else base.recipient_allowlist,
            endpoint_allowlist=_set(d["endpoint_allowlist"]) if "endpoint_allowlist" in d else base.endpoint_allowlist,
            daily_quota=int(d.get("daily_quota", base.daily_quota)),
        )


# --- The tenant scope -------------------------------------------------------


@dataclass(frozen=True)
class TenantScope:
    """Everything an agent needs to run within ONE tenant. Built only via
    :func:`resolve_scope`; immutable; never shared across tenants."""

    tenant_id: str
    credential: str
    mcp_url: str
    state_root: Path
    notes_inbox: Path
    action_outbox: Path
    egress: EgressSettings

    # -- per-tenant governance paths (all under state_root) ------------------

    @property
    def runs_dir(self) -> Path:
        return self.state_root / "runs"

    @property
    def audit_dir(self) -> Path:
        return self.runs_dir

    @property
    def proposals_dir(self) -> Path:
        return self.runs_dir

    @property
    def connector_state_dir(self) -> Path:
        return self.runs_dir / "connectors"

    @property
    def dead_letter_dir(self) -> Path:
        return self.connector_state_dir

    @property
    def egress_state_dir(self) -> Path:
        return self.state_root / "egress"

    @property
    def send_audit_file(self) -> Path:
        return self.egress_state_dir / "send_audit.jsonl"

    @property
    def egress_ledger(self) -> Path:
        return self.egress_state_dir / "egress.json"

    @property
    def email_sent_store(self) -> Path:
        return self.egress_state_dir / "email_sent.json"

    @property
    def checkpoint_db(self) -> Path:
        return self.state_root / "checkpoints.db"

    @property
    def notify_file(self) -> Path:
        return self.action_outbox / "notifications.jsonl"

    def ensure_dirs(self) -> None:
        for d in (self.state_root, self.runs_dir, self.connector_state_dir,
                  self.egress_state_dir, self.notes_inbox, self.action_outbox):
            d.mkdir(parents=True, exist_ok=True)

    # -- scope-bound factories (every store/connection is this tenant's) -----

    def knowledge_source(self):
        from .knowledge import mnesis_mcp_source

        return mnesis_mcp_source(url=self.mcp_url, token=self.credential)

    def audit_log(self):
        from .audit import AgentAuditLog

        return AgentAuditLog(self.audit_dir)

    def proposal_store(self):
        from .proposals import ProposalStore

        return ProposalStore(self.proposals_dir)

    def action_proposal_store(self):
        from .proposals import ActionProposalStore

        return ActionProposalStore(self.proposals_dir)

    def report_store(self):
        from .reports import DreamReportStore

        return DreamReportStore(self.proposals_dir, audit=self.audit_log())

    def processed_store(self, name: str = "notes.sqlite"):
        from .triggers.connector import ProcessedStore

        return ProcessedStore(self.connector_state_dir / name)

    def dead_letter_store(self):
        from .writing_pipeline import DeadLetterStore

        return DeadLetterStore(self.dead_letter_dir)

    def egress_policy(self):
        from .egress import EgressPolicy, EgressQuotaStore

        e = self.egress
        return EgressPolicy(
            enabled=e.enabled, kill=e.kill,
            recipient_allowlist=e.recipient_allowlist, endpoint_allowlist=e.endpoint_allowlist,
            rate_limit=e.rate_limit, rate_window_seconds=e.rate_window_seconds,
            daily_quota=e.daily_quota, global_rate_limit=e.global_rate_limit,
            global_daily_quota=e.global_daily_quota,
            quota_store=EgressQuotaStore(self.egress_ledger),
        )

    def send_audit(self):
        from .send_audit import SendAuditLog

        return SendAuditLog(self.send_audit_file)

    def channel_registry(self):
        """The tenant's outbound channels: its own inert draft outbox + local notify,
        plus its own (egress-gated, send-audited) email channel iff enabled."""
        from .channels import ChannelRegistry, DraftOutboxChannel, LocalNotifyChannel

        channels = [DraftOutboxChannel(self.action_outbox), LocalNotifyChannel(self.notify_file)]
        if self.egress.email_enabled:
            from .email_channel import EmailSendChannel, _SentStore

            channels.append(EmailSendChannel(
                egress=self.egress_policy(), dryrun=self.egress.dryrun,
                sent_store=_SentStore(self.email_sent_store), send_audit=self.send_audit(),
            ))
        return ChannelRegistry(channels)

    def action_gate(self):
        """The tenant's approval gate over its own channels, proposals, audit, egress."""
        from .action_gate import ActionGate

        return ActionGate(
            self.channel_registry(), store=self.action_proposal_store(),
            audit=self.audit_log(), egress=self.egress_policy(),
        )


# --- Resolution (fail-closed) ----------------------------------------------


def resolve_scope(
    tenant_id: str | None,
    credential: str | None,
    *,
    mcp_url: str | None = None,
    state_base: Path | str | None = None,
    notes_inbox: Path | str | None = None,
    action_outbox: Path | str | None = None,
    egress: EgressSettings | dict | None = None,
) -> TenantScope:
    """Resolve a :class:`TenantScope`. **Fail-closed**: a missing tenant id or
    credential raises :class:`UnresolvedTenant`, so an agent that cannot resolve its
    tenant credential does not start."""
    if not tenant_id:
        raise UnresolvedTenant("no tenant id for the agent scope")
    if not credential:
        raise UnresolvedTenant(
            f"tenant {tenant_id!r} has no MCP credential — agents will not start (fail closed)"
        )
    base = Path(state_base).expanduser() if state_base is not None else _state_base()
    root = base / "tenants" / tenant_id
    eg = egress if isinstance(egress, EgressSettings) else EgressSettings.from_dict(egress)
    return TenantScope(
        tenant_id=tenant_id,
        credential=credential,
        mcp_url=mcp_url or config.MNESIS_MCP_URL,
        state_root=root,
        notes_inbox=Path(notes_inbox).expanduser() if notes_inbox else (config.MNESIS_NOTES_INBOX / tenant_id),
        action_outbox=Path(action_outbox).expanduser() if action_outbox else (config.MNESIS_ACTION_OUTBOX / tenant_id),
        egress=eg,
    )


def load_scopes() -> list[TenantScope]:
    """The tenant scopes the runtime hosts. Sources, in order:

    1. ``MNESIS_AGENTS_TENANTS_FILE`` — a JSON ``{"tenants": [ {tenant_id, credential,
       mcp_url?, notes_inbox?, action_outbox?, egress?}, … ]}`` (per-tenant config).
       A tenant without a credential is **fail-closed** (raises).
    2. else a single legacy scope from ``MNESIS_MCP_TOKEN`` (tenant id
       ``MNESIS_AGENTS_TENANT_ID``, default ``default``) — the single-tenant path.
    3. else ``[]`` — nothing resolvable; the runner comes up idle.
    """
    path = os.environ.get("MNESIS_AGENTS_TENANTS_FILE")
    if path and Path(path).is_file():
        data = json.loads(Path(path).read_text(encoding="utf-8") or "{}")
        entries = data.get("tenants") if isinstance(data, dict) else data
        scopes: list[TenantScope] = []
        for entry in entries or []:
            scopes.append(resolve_scope(
                entry.get("tenant_id"), entry.get("credential"),
                mcp_url=entry.get("mcp_url"), notes_inbox=entry.get("notes_inbox"),
                action_outbox=entry.get("action_outbox"), egress=entry.get("egress"),
            ))
        return scopes
    token = config.MNESIS_MCP_TOKEN
    if token:
        tid = os.environ.get("MNESIS_AGENTS_TENANT_ID", "default")
        return [resolve_scope(tid, token)]
    return []
