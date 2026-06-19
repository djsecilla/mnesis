"""Tests for the consolidated send-time guardrails (E4): immutable send-audit,
quota enforcement at send time, last-moment kill-switch, and crash-safe
at-most-once idempotency. Mock transport; no real network.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from mnesis_agents.action_gate import ActionGate
from mnesis_agents.audit import AgentAuditLog
from mnesis_agents.channels import ChannelRegistry, OutboundArtifact
from mnesis_agents.egress import EgressPolicy, EgressQuotaStore, Recipient
from mnesis_agents.email_channel import (
    AmbiguousSendError,
    EmailSendChannel,
    _IN_FLIGHT,
    _SentStore,
)
from mnesis_agents.proposals import ActionProposalStore
from mnesis_agents.send_audit import SendAuditLog

_NOW = datetime(2026, 6, 19, 12, 0, 0, tzinfo=timezone.utc)
_RCPT = "ops@example.com"
_EP = "smtp.example.com:587"


class _Transport:
    def __init__(self, *, raises=None):
        self.calls = []
        self.raises = raises

    def __call__(self, **kw):
        self.calls.append(kw)
        if self.raises is not None:
            raise self.raises


def _egress(tmp_path, **kw):
    base = dict(
        enabled=True, recipient_allowlist=frozenset({_RCPT}), endpoint_allowlist=frozenset({_EP}),
        rate_limit=20, daily_quota=20, global_rate_limit=50, global_daily_quota=100,
        quota_store=EgressQuotaStore(tmp_path / "egress.json"),
    )
    base.update(kw)
    return EgressPolicy(**base)


def _channel(tmp_path, *, egress, transport, audit=None, sent_store=None, dryrun=False):
    return EmailSendChannel(
        egress=egress, dryrun=dryrun, host="smtp.example.com", port=587,
        sender="mnesis@example.com", transport=transport,
        sent_store=sent_store or _SentStore(tmp_path / "sent.json"),
        send_audit=audit or SendAuditLog(tmp_path / "send_audit.jsonl"),
    )


def _art(body="Atlas uses Redis for caching."):
    return OutboundArtifact(kind="brief", title="Atlas Brief", body=body)


def _ctx(pid="p1", **kw):
    base = dict(recipient_source="policy", proposal_id=pid, approval_id=f"a-{pid}", now=_NOW)
    base.update(kw)
    return base


# ── immutable send-audit ────────────────────────────────────────────────────


def test_a_send_writes_one_immutable_audit_record_without_body(tmp_path):
    audit = SendAuditLog(tmp_path / "send_audit.jsonl")
    ch = _channel(tmp_path, egress=_egress(tmp_path), transport=_Transport(), audit=audit)
    secret_in_body = "Atlas uses Redis. ZZZSECRETBODY."
    res = ch.deliver(_art(body=secret_in_body), _RCPT, _ctx())
    assert res.status == "sent"

    records = audit.all()
    assert len(records) == 1                         # exactly one record per attempt
    r = records[0]
    # The documented fields are present…
    for f in ("proposal_id", "approval_id", "channel", "recipient", "endpoint",
              "content_hash", "decision", "status", "ts", "prev_hash", "hash"):
        assert f in r
    assert r["recipient"] == _RCPT and r["endpoint"] == _EP and r["status"] == "sent"
    assert r["content_hash"].startswith("sha256:")
    # …and the BODY / secret is never in the audit.
    assert "ZZZSECRETBODY" not in json.dumps(records)

    # Tamper-evident: the chain verifies, and an edit is detected.
    assert audit.verify() == (True, None)


def test_audit_chain_detects_tampering(tmp_path):
    audit = SendAuditLog(tmp_path / "send_audit.jsonl")
    ch = _channel(tmp_path, egress=_egress(tmp_path), transport=_Transport(), audit=audit)
    ch.deliver(_art(), _RCPT, _ctx("pa"))
    ch.deliver(_art(), _RCPT, _ctx("pb"))
    assert audit.verify()[0] is True

    # Tamper: flip a status in the on-disk log.
    lines = (tmp_path / "send_audit.jsonl").read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[0])
    rec["status"] = "blocked"
    lines[0] = json.dumps(rec)
    (tmp_path / "send_audit.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    ok, broken_at = audit.verify()
    assert ok is False and broken_at == 0


# ── quota at send time ──────────────────────────────────────────────────────


def test_exceeding_a_quota_denies_at_send_time(tmp_path):
    eg = _egress(tmp_path, daily_quota=2)
    transport = _Transport()
    audit = SendAuditLog(tmp_path / "send_audit.jsonl")

    def send(pid):
        ch = _channel(tmp_path, egress=eg, transport=transport, audit=audit,
                      sent_store=_SentStore(tmp_path / f"sent-{pid}.json"))
        return ch.deliver(_art(), _RCPT, _ctx(pid))

    assert send("p1").status == "sent"
    assert send("p2").status == "sent"
    third = send("p3")
    assert third.status == "blocked" and "quota" in third.detail.lower()
    assert len(transport.calls) == 2                 # the over-quota send did not transmit
    # The denial is recorded in the immutable audit.
    blocked = [r for r in audit.all() if r["status"] == "blocked"]
    assert blocked and "quota" in blocked[0]["decision"].lower()


# ── last-moment kill-switch (after approval) ────────────────────────────────


def test_kill_switch_engaged_after_approval_blocks_the_send(tmp_path):
    eg = _egress(tmp_path)
    transport = _Transport()
    gate = ActionGate(
        ChannelRegistry([_channel(tmp_path, egress=eg, transport=transport, dryrun=False)]),
        store=ActionProposalStore(tmp_path), audit=AgentAuditLog(tmp_path), egress=eg,
    )
    p = gate.propose(action_type="email-brief", channel="email", artifact=_art(),
                     destination=_RCPT, rationale="sync")
    # The operator engages the kill-switch AFTER the proposal/approval is set up.
    eg.kill = True
    res = gate.approve(p.id, confirm_recipient=_RCPT)
    assert res.status == "blocked" and "kill" in res.detail.lower()
    assert transport.calls == []                     # the last-moment check halted it


# ── at-most-once idempotency ────────────────────────────────────────────────


def test_same_approved_proposal_cannot_send_twice(tmp_path):
    transport = _Transport()
    store = _SentStore(tmp_path / "sent.json")
    ch = _channel(tmp_path, egress=_egress(tmp_path), transport=transport, sent_store=store)
    a = ch.deliver(_art(), _RCPT, _ctx("dup"))
    b = ch.deliver(_art(), _RCPT, _ctx("dup"))       # same send key
    assert a.status == "sent" and b.status == "sent"
    assert len(transport.calls) == 1                 # at-most-once


def test_mid_send_crash_resolves_to_needs_human_not_a_resend(tmp_path):
    transport = _Transport(raises=SystemExit("process died mid-send"))  # BaseException = crash
    store = _SentStore(tmp_path / "sent.json")
    ch = _channel(tmp_path, egress=_egress(tmp_path), transport=transport, sent_store=store)

    # The crash propagates (not caught) — the key was marked in_flight before transmit.
    with pytest.raises(SystemExit):
        ch.deliver(_art(), _RCPT, _ctx("crash"))
    assert store.state("crash") == _IN_FLIGHT
    assert len(transport.calls) == 1

    # A duplicate path with the same key resolves to needs_human — NOT a resend.
    healthy = _Transport()
    ch2 = _channel(tmp_path, egress=_egress(tmp_path), transport=healthy, sent_store=store,
                   audit=SendAuditLog(tmp_path / "audit2.jsonl"))
    res = ch2.deliver(_art(), _RCPT, _ctx("crash"))
    assert res.status == "needs_human" and healthy.calls == []


def test_ambiguous_then_repeat_does_not_resend(tmp_path):
    transport = _Transport(raises=AmbiguousSendError("dropped after DATA"))
    store = _SentStore(tmp_path / "sent.json")
    ch = _channel(tmp_path, egress=_egress(tmp_path), transport=transport, sent_store=store)
    first = ch.deliver(_art(), _RCPT, _ctx("amb"))
    assert first.status == "needs_human" and len(transport.calls) == 1
    again = ch.deliver(_art(), _RCPT, _ctx("amb"))
    assert again.status == "needs_human" and len(transport.calls) == 1


def test_clean_failure_does_not_block_a_later_legit_attempt(tmp_path):
    # A clean failure (definitely not sent) clears the key, so it is not a permanent
    # needs_human; the proposal is terminal at the gate anyway, but the channel
    # itself never falsely reports a non-send as sent.
    store = _SentStore(tmp_path / "sent.json")
    ch = _channel(tmp_path, egress=_egress(tmp_path),
                  transport=_Transport(raises=RuntimeError("connection refused")), sent_store=store)
    res = ch.deliver(_art(), _RCPT, _ctx("cf"))
    assert res.status == "failed" and store.state("cf") is None
