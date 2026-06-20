"""E6 — deployment posture + staged-rollout verification for the email channel.

These are the acceptance "drills" for shipping the external email channel safely:

  * **Default-off posture** — with the shipped defaults (no egress env set), the
    egress plane is disabled, the email channel is not even registered, and email
    is dry-run; nothing can egress.
  * **Stage 1 (dry-run only)** — email enabled but dry-run: renders, sends nothing.
  * **Stage 2 (self-send)** — enable egress + allowlist ONLY the operator's own
    address: exactly one real send (mock SMTP) to self, audited; any other
    recipient is refused even though egress is on.
  * **Stage 3 (add a recipient)** — adding a second allowlisted address lets it send.
  * **Safety drills** — a planted secret is blocked; a non-allowlisted recipient is
    refused; the kill-switch halts a post-approval send; a quota halts sends; an
    approved proposal sends at most once.

Offline: mock SMTP transport, injected clock + temp ledgers. No real network.
(The real-operator self-send is the documented manual drill in docs/OPS.md.)
"""
from __future__ import annotations

from datetime import datetime, timezone

from mnesis_agents import config
from mnesis_agents.channels import RISK_EXTERNAL, OutboundArtifact
from mnesis_agents.egress import EgressPolicy, EgressQuotaStore, Recipient
from mnesis_agents.email_channel import (
    EmailSendChannel,
    _SentStore,
    action_channel_registry,
)
from mnesis_agents.send_audit import SendAuditLog

_NOW = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)
_OPERATOR = "me@example.com"
_TEAMMATE = "teammate@example.com"
_HOST, _PORT = "smtp.example.com", 587
_ENDPOINT = f"{_HOST}:{_PORT}"


class _MockTransport:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, **kw):
        self.calls.append(kw)


def _egress(tmp_path, **kw) -> EgressPolicy:
    base = dict(
        enabled=True, recipient_allowlist=frozenset({_OPERATOR}),
        endpoint_allowlist=frozenset({_ENDPOINT}),
        rate_limit=10, daily_quota=10, global_rate_limit=20, global_daily_quota=50,
        quota_store=EgressQuotaStore(tmp_path / "egress.json"),
    )
    base.update(kw)
    return EgressPolicy(**base)


def _channel(tmp_path, *, dryrun, egress, transport, audit=None):
    return EmailSendChannel(
        egress=egress, dryrun=dryrun, host=_HOST, port=_PORT, sender=_OPERATOR,
        transport=transport, sent_store=_SentStore(tmp_path / "sent.json"),
        send_audit=audit or SendAuditLog(tmp_path / "audit.jsonl"),
    )


def _artifact(body="Atlas uses Redis for caching. Coordinate with Sarah."):
    return OutboundArtifact(kind="brief", title="Atlas Brief", body=body)


def _ctx(recipient=_OPERATOR, **kw):
    base = dict(recipient_source="policy", proposal_id="p1", now=_NOW)
    base.update(kw)
    return base


# ── default-off posture (the shipped defaults) ──────────────────────────────


def test_shipped_defaults_are_off(monkeypatch):
    # The three switches that gate any external send all ship safe.
    assert config.MNESIS_EGRESS_ENABLED is False     # default-deny egress
    assert config.MNESIS_EMAIL_ENABLED is False      # email channel not registered
    assert config.MNESIS_EMAIL_DRYRUN is True        # email renders, sends nothing


def test_default_config_egress_denies_everything(monkeypatch, tmp_path):
    # With no egress configured, from_config() is default-deny: every send denied.
    monkeypatch.setattr(config, "MNESIS_EGRESS_ENABLED", False)
    monkeypatch.setattr(config, "MNESIS_EGRESS_KILL", False)
    monkeypatch.setattr(config, "MNESIS_EGRESS_RECIPIENT_ALLOWLIST", "")
    monkeypatch.setattr(config, "MNESIS_EGRESS_ENDPOINT_ALLOWLIST", "")
    monkeypatch.setattr(config, "MNESIS_EGRESS_STATE_DIR", tmp_path)
    policy = EgressPolicy.from_config()
    decision = policy.check_send_allowed(
        RISK_EXTERNAL, Recipient(_OPERATOR, "policy"), _ENDPOINT, now=_NOW)
    assert decision.denied and "disabled" in decision.reason


def test_default_action_registry_has_no_email_channel(monkeypatch):
    monkeypatch.setattr(config, "MNESIS_EMAIL_ENABLED", False)
    reg = action_channel_registry()
    assert "email" not in reg.names()                # not a delivery option by default


def test_email_with_egress_off_can_only_dry_run(tmp_path):
    # Even an "enabled" channel cannot send while the egress plane is off: dry-run
    # default true → dry_run; and with dry-run forced off, egress-disabled blocks.
    transport = _MockTransport()
    eg_off = _egress(tmp_path, enabled=False)
    assert _channel(tmp_path, dryrun=True, egress=eg_off, transport=transport).deliver(
        _artifact(), _OPERATOR, _ctx()).status == "dry_run"
    assert transport.calls == []
    assert _channel(tmp_path, dryrun=False, egress=eg_off, transport=transport).deliver(
        _artifact(), _OPERATOR, _ctx(proposal_id="p2")).status == "blocked"
    assert transport.calls == []                     # nothing egressed


# ── stage 1: dry-run only ───────────────────────────────────────────────────


def test_stage1_dry_run_renders_without_sending(tmp_path):
    transport = _MockTransport()
    res = _channel(tmp_path, dryrun=True, egress=_egress(tmp_path), transport=transport).deliver(
        _artifact(), _OPERATOR, _ctx())
    assert res.status == "dry_run" and res.recipient == _OPERATOR
    assert transport.calls == []


# ── stage 2: self-send (operator-only allowlist, live) ──────────────────────


def test_stage2_self_send_delivers_exactly_once_and_audits(tmp_path):
    transport = _MockTransport()
    audit = SendAuditLog(tmp_path / "audit.jsonl")
    ch = _channel(tmp_path, dryrun=False, egress=_egress(tmp_path), transport=transport, audit=audit)
    res = ch.deliver(_artifact(), _OPERATOR, _ctx(proposal_id="self"))

    assert res.status == "sent" and len(transport.calls) == 1
    assert transport.calls[0]["recipient"] == _OPERATOR
    records = audit.all()
    assert len(records) == 1 and records[0]["status"] == "sent"
    assert records[0]["recipient"] == _OPERATOR and audit.verify()[0] is True
    # The body never appears in the audit record.
    assert "Atlas uses Redis" not in "".join(str(v) for v in records[0].values())


def test_stage2_any_other_recipient_is_refused_even_when_enabled(tmp_path):
    transport = _MockTransport()
    # Operator-only allowlist; a teammate is NOT allowlisted yet → blocked.
    res = _channel(tmp_path, dryrun=False, egress=_egress(tmp_path), transport=transport).deliver(
        _artifact(), _TEAMMATE, _ctx(recipient=_TEAMMATE, proposal_id="t1"))
    assert res.status == "blocked" and transport.calls == []


# ── stage 3: add a further allowlisted recipient, one at a time ─────────────


def test_stage3_adding_a_recipient_lets_it_send(tmp_path):
    transport = _MockTransport()
    egress = _egress(tmp_path, recipient_allowlist=frozenset({_OPERATOR, _TEAMMATE}))
    res = _channel(tmp_path, dryrun=False, egress=egress, transport=transport).deliver(
        _artifact(), _TEAMMATE, _ctx(recipient=_TEAMMATE, proposal_id="t2"))
    assert res.status == "sent" and transport.calls[0]["recipient"] == _TEAMMATE


# ── safety drills (acceptance) ──────────────────────────────────────────────


def test_planted_payload_secret_is_blocked(tmp_path):
    transport = _MockTransport()
    res = _channel(tmp_path, dryrun=False, egress=_egress(tmp_path), transport=transport).deliver(
        _artifact(body="key: AKIAIOSFODNN7EXAMPLE — rotate it"), _OPERATOR, _ctx(proposal_id="sec"))
    assert res.status == "blocked" and transport.calls == []
    assert "AKIAIOSFODNN7EXAMPLE" not in (res.detail + (res.error or ""))


def test_kill_switch_halts_a_post_approval_send(tmp_path):
    transport = _MockTransport()
    egress = _egress(tmp_path)
    ch = _channel(tmp_path, dryrun=False, egress=egress, transport=transport)
    egress.kill = True                               # engaged AFTER approval/setup
    res = ch.deliver(_artifact(), _OPERATOR, _ctx(proposal_id="k1"))
    assert res.status == "blocked" and "kill" in res.detail.lower()
    assert transport.calls == []


def test_quota_limit_halts_sends(tmp_path):
    transport = _MockTransport()
    egress = _egress(tmp_path, daily_quota=1)
    ch = _channel(tmp_path, dryrun=False, egress=egress, transport=transport)
    first = ch.deliver(_artifact(), _OPERATOR, _ctx(proposal_id="q1"))
    second = ch.deliver(_artifact(), _OPERATOR, _ctx(proposal_id="q2"))
    assert first.status == "sent"
    assert second.status == "blocked" and "quota" in second.detail.lower()
    assert len(transport.calls) == 1                 # the over-quota send did not transmit


def test_approved_proposal_sends_at_most_once(tmp_path):
    transport = _MockTransport()
    ch = _channel(tmp_path, dryrun=False, egress=_egress(tmp_path), transport=transport)
    a = ch.deliver(_artifact(), _OPERATOR, _ctx(proposal_id="once"))
    b = ch.deliver(_artifact(), _OPERATOR, _ctx(proposal_id="once"))   # same key
    assert a.status == "sent" and b.status == "sent"
    assert len(transport.calls) == 1                 # at-most-once
