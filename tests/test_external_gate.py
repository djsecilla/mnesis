"""Tests for the recipient-confirmation gate for external sends (E3).

Offline. An external proposal shows the recipient + a dry-run preview; content-only
approval does not send; a matching confirm_recipient proceeds; a mismatched or
content-sourced confirmation is refused; editing to a non-allowlisted recipient is
refused; and no policy can auto-approve an external proposal.
"""
from __future__ import annotations

import pytest

from mnesis_agents.action_gate import (
    ActionGate,
    ActionPolicy,
    GateError,
    RecipientConfirmationError,
)
from mnesis_agents.audit import AgentAuditLog
from mnesis_agents.channels import (
    ChannelRegistry,
    DraftOutboxChannel,
    OutboundArtifact,
)
from mnesis_agents.egress import EgressPolicy, EgressQuotaStore, Recipient
from mnesis_agents.email_channel import EmailSendChannel, _SentStore
from mnesis_agents.proposals import ActionProposalStore

_RCPT = "ops@example.com"
_RCPT2 = "ops2@example.com"
_EP = "smtp.example.com:587"


class _MockTransport:
    def __init__(self):
        self.calls = []

    def __call__(self, **kw):
        self.calls.append(kw)


def _egress(tmp_path, allow=(_RCPT,)):
    return EgressPolicy(
        enabled=True, recipient_allowlist=frozenset(allow), endpoint_allowlist=frozenset({_EP}),
        rate_limit=20, daily_quota=20, global_rate_limit=50, global_daily_quota=100,
        quota_store=EgressQuotaStore(tmp_path / "egress.json"),
    )


def _gate(tmp_path, *, dryrun=True, transport=None, egress=None, policy=None):
    eg = egress or _egress(tmp_path)
    email = EmailSendChannel(
        egress=eg, dryrun=dryrun, host="smtp.example.com", port=587,
        sender="mnesis@example.com", transport=transport, sent_store=_SentStore(tmp_path / "sent.json"),
    )
    channels = ChannelRegistry([email, DraftOutboxChannel(tmp_path / "outbox")])
    return ActionGate(
        channels, store=ActionProposalStore(tmp_path), audit=AgentAuditLog(tmp_path),
        egress=eg, policy=policy or ActionPolicy(),
    )


def _artifact(body="Atlas uses Redis for caching."):
    return OutboundArtifact(kind="brief", title="Atlas Brief", body=body,
                            metadata={"citations": ["atlas-redis"]})


def _propose(gate, *, destination=_RCPT):
    return gate.propose(action_type="email-brief", channel="email",
                        artifact=_artifact(), destination=destination, rationale="weekly sync")


# ── presentation ────────────────────────────────────────────────────────────


def test_external_proposal_shows_recipient_and_dry_run_preview(tmp_path):
    gate = _gate(tmp_path)
    p = _propose(gate)
    view = gate.present(p.id)

    assert view["risk_class"] == "external"
    assert view["recipient"] == _RCPT and view["endpoint"] == _EP
    assert view["recipient_confirmation_required"] is True
    assert view["recipient_allowlisted"] is True
    assert view["rationale"] == "weekly sync" and view["citations"] == ["atlas-redis"]
    preview = view["dry_run_preview"]
    assert "Atlas uses Redis" in preview["body"]          # the exact message, for the human
    assert preview["recipient"] == _RCPT and preview["content_hash"].startswith("sha256:")
    assert preview["secret_findings"] == []


# ── recipient confirmation ──────────────────────────────────────────────────


def test_content_only_approval_does_not_send(tmp_path):
    transport = _MockTransport()
    gate = _gate(tmp_path, dryrun=False, transport=transport)
    p = _propose(gate)
    # No confirm_recipient → refused; nothing sent; proposal stays pending.
    with pytest.raises(RecipientConfirmationError) as ei:
        gate.approve(p.id)
    assert "confirmation" in str(ei.value)
    assert transport.calls == [] and gate.store.get(p.id).status == "pending"


def test_matching_confirmation_proceeds(tmp_path):
    gate = _gate(tmp_path, dryrun=True)   # dry-run mode
    p = _propose(gate)
    res = gate.approve(p.id, confirm_recipient=_RCPT)
    assert res.status == "dry_run"        # proceeded (to dry-run per channel mode)
    stored = gate.store.get(p.id)
    assert stored.status == "dry_run" and stored.recipient_confirmed is True


def test_matching_confirmation_sends_in_live_mode(tmp_path):
    transport = _MockTransport()
    gate = _gate(tmp_path, dryrun=False, transport=transport)
    p = _propose(gate)
    res = gate.approve(p.id, confirm_recipient=_RCPT)
    assert res.status == "sent" and len(transport.calls) == 1
    assert gate.store.get(p.id).status == "executed"


def test_mismatched_confirmation_is_refused(tmp_path):
    transport = _MockTransport()
    gate = _gate(tmp_path, dryrun=False, transport=transport)
    p = _propose(gate)
    with pytest.raises(RecipientConfirmationError) as ei:
        gate.approve(p.id, confirm_recipient="someone-else@example.com")
    assert "does not match" in str(ei.value)
    assert transport.calls == [] and gate.store.get(p.id).status == "pending"


def test_content_sourced_confirmation_is_refused(tmp_path):
    transport = _MockTransport()
    gate = _gate(tmp_path, dryrun=False, transport=transport)
    p = _propose(gate)
    # Even the exact, allowlisted address — if its source is 'content' → refused.
    with pytest.raises(RecipientConfirmationError) as ei:
        gate.approve(p.id, confirm_recipient=Recipient(_RCPT, "content"))
    assert "policy/user" in str(ei.value)
    assert transport.calls == []


# ── editing the recipient re-runs E1 ────────────────────────────────────────


def test_editing_to_a_non_allowlisted_recipient_is_refused(tmp_path):
    transport = _MockTransport()
    gate = _gate(tmp_path, dryrun=False, transport=transport)
    p = _propose(gate)
    evil = "attacker@evil.com"
    with pytest.raises(RecipientConfirmationError) as ei:
        gate.approve(p.id, edited_destination=evil, confirm_recipient=evil)
    assert "egress" in str(ei.value).lower()       # E1 re-validation refused it
    assert transport.calls == [] and gate.store.get(p.id).status == "pending"


def test_editing_to_another_allowlisted_recipient_proceeds(tmp_path):
    transport = _MockTransport()
    gate = _gate(tmp_path, dryrun=False, transport=transport, egress=_egress(tmp_path, allow=(_RCPT, _RCPT2)))
    p = _propose(gate)
    res = gate.approve(p.id, edited_destination=_RCPT2, confirm_recipient=_RCPT2)
    assert res.status == "sent"
    assert transport.calls[0]["recipient"] == _RCPT2
    assert gate.store.get(p.id).destination == _RCPT2 and gate.store.get(p.id).edited is True


# ── external is ALWAYS gated — no auto-approve path ─────────────────────────


def test_no_policy_can_auto_approve_an_external_proposal(tmp_path):
    transport = _MockTransport()
    # The escape hatch is ON — but it can only ever apply to INERT channels.
    gate = _gate(tmp_path, dryrun=False, transport=transport, policy=ActionPolicy(auto_run_inert=True))
    p = _propose(gate)
    assert p.status == "pending"          # gated, not auto-run, despite the flag
    assert transport.calls == []
    # It still requires explicit recipient confirmation to ever send.
    gate.approve(p.id, confirm_recipient=_RCPT)
    assert len(transport.calls) == 1


# ── reject / expire ─────────────────────────────────────────────────────────


def test_reject_and_expire_deliver_nothing(tmp_path):
    transport = _MockTransport()
    gate = _gate(tmp_path, dryrun=False, transport=transport)
    p1 = _propose(gate)
    gate.reject(p1.id, "not now")
    assert gate.store.get(p1.id).status == "rejected"

    p2 = _propose(gate)
    gate.expire(p2.id)
    assert gate.store.get(p2.id).status == "expired"
    assert transport.calls == []
    # Neither can then be approved.
    for pid in (p1.id, p2.id):
        with pytest.raises(GateError):
            gate.approve(pid, confirm_recipient=_RCPT)


# ── inert proposals do NOT require confirmation (no regression) ─────────────


def test_inert_proposal_needs_no_recipient_confirmation(tmp_path):
    gate = _gate(tmp_path)
    p = gate.propose(action_type="brief", channel="draft-outbox",
                     artifact=_artifact(), destination="operator")
    res = gate.approve(p.id)              # no confirm_recipient needed for inert
    assert res.ok and gate.store.get(p.id).status == "executed"


# ── audit records the recipient + content hash ──────────────────────────────


def test_audit_records_recipient_and_content_hash(tmp_path):
    import json
    import os

    transport = _MockTransport()
    gate = _gate(tmp_path, dryrun=False, transport=transport)
    p = _propose(gate)
    gate.approve(p.id, confirm_recipient=_RCPT)

    records = []
    for f in [f for f in os.listdir(tmp_path) if f.startswith("runs-")]:
        records += [json.loads(line) for line in open(tmp_path / f, encoding="utf-8") if line.strip()]
    executed = [r for r in records if r.get("type") == "action_event" and r["event"] == "executed"]
    assert executed
    ev = executed[0]
    assert ev["destination"] == _RCPT and ev["recipient_confirmed"] is True
    assert ev["result_content_hash"] and ev["result_content_hash"].startswith("sha256:")
    # The body is never in the audit.
    assert "Atlas uses Redis" not in json.dumps(records)
