"""Tests for the approval gate + action proposals (A2) — the safety keystone.

Offline, temp dirs. Validates: composing an action creates a proposal and PAUSES
(nothing delivered); approving executes the channel exactly once; rejecting
discards; editing changes the delivered artifact; a destination sourced from
artifact content is refused; an EXTERNAL channel can never auto-run even with the
auto-run flag on; and every outcome is audited without the artifact body.
"""
from __future__ import annotations

import json
import os

import pytest

from mnesis_agents.action_gate import (
    ActionGate,
    ActionPolicy,
    DestinationIntegrityError,
    GateError,
)
from mnesis_agents.audit import AgentAuditLog
from mnesis_agents.channels import (
    RISK_EXTERNAL,
    RISK_INERT,
    ChannelRegistry,
    DraftOutboxChannel,
    LocalNotifyChannel,
    OutboundArtifact,
    OutboundChannel,
)
from mnesis_agents.proposals import ActionProposalStore


class _FakeExternalChannel(OutboundChannel):
    """A test-only EXTERNAL channel — records sends so we can assert it never runs
    un-gated (no real external send ships in the codebase)."""

    name = "ext-send"
    risk_class = RISK_EXTERNAL

    def __init__(self) -> None:
        self.sent: list = []

    def deliver(self, artifact, destination=None, context=None):
        self.sent.append((artifact, destination))
        return self._ok(destination=destination, location="EXTERNAL-SENT", detail="sent")


def _gate(tmp_path, *, policy=None, external=None):
    channels = [DraftOutboxChannel(tmp_path / "outbox"), LocalNotifyChannel(tmp_path / "n.jsonl")]
    if external is not None:
        channels.append(external)
    return ActionGate(
        ChannelRegistry(channels),
        store=ActionProposalStore(tmp_path),
        audit=AgentAuditLog(tmp_path),
        policy=policy,
    )


def _artifact(body="Atlas uses Redis for caching.", meta=None):
    return OutboundArtifact(kind="brief", title="Atlas Brief", body=body, metadata=meta or {"pages": ["atlas"]})


def _drafts(tmp_path):
    return sorted((tmp_path / "outbox").glob("*.md"))


# ── propose → pause ─────────────────────────────────────────────────────────


def test_composing_an_action_creates_a_proposal_and_pauses(tmp_path):
    gate = _gate(tmp_path)
    p = gate.propose(action_type="draft-brief", channel="draft-outbox",
                     artifact=_artifact(), destination="operator", rationale="because")
    assert p.status == "pending" and p.risk_class == RISK_INERT
    assert p.channel == "draft-outbox" and p.destination == "operator"
    # PAUSED: nothing was delivered.
    assert _drafts(tmp_path) == []
    # The proposal is persisted and listable.
    assert gate.store.get(p.id).status == "pending"
    assert [x.id for x in gate.store.list_pending()] == [p.id]


def test_with_no_approval_nothing_is_delivered(tmp_path):
    gate = _gate(tmp_path)
    gate.propose(action_type="a", channel="draft-outbox", artifact=_artifact(), destination="op")
    # Simply never approving → no side effect ever happens.
    assert _drafts(tmp_path) == []


# ── approve / reject / edit ─────────────────────────────────────────────────


def test_approving_executes_the_channel_exactly_once(tmp_path):
    gate = _gate(tmp_path)
    p = gate.propose(action_type="a", channel="draft-outbox", artifact=_artifact(), destination="op")
    res = gate.approve(p.id)
    assert res.ok and res.channel == "draft-outbox"
    assert len(_drafts(tmp_path)) == 1
    assert gate.store.get(p.id).status == "executed"
    # A proposal executes at most once — re-approval is refused, no second draft.
    with pytest.raises(GateError):
        gate.approve(p.id)
    assert len(_drafts(tmp_path)) == 1


def test_rejecting_discards_with_nothing_delivered(tmp_path):
    gate = _gate(tmp_path)
    p = gate.propose(action_type="a", channel="draft-outbox", artifact=_artifact(), destination="op")
    updated = gate.reject(p.id, reason="not now")
    assert updated.status == "rejected" and updated.decision_note == "not now"
    assert _drafts(tmp_path) == []
    # A rejected proposal cannot then be approved.
    with pytest.raises(GateError):
        gate.approve(p.id)


def test_editing_changes_the_delivered_artifact(tmp_path):
    gate = _gate(tmp_path)
    p = gate.propose(action_type="a", channel="draft-outbox",
                     artifact=_artifact(body="ORIGINAL body"), destination="op")
    res = gate.approve(
        p.id, edited_artifact={"title": "Edited Title", "body": "EDITED body"},
        edited_destination="operator-2",
    )
    assert res.ok
    text = _drafts(tmp_path)[0].read_text(encoding="utf-8")
    assert "EDITED body" in text and "ORIGINAL body" not in text
    assert "Edited Title" in text and "destination: operator-2" in text
    stored = gate.store.get(p.id)
    assert stored.status == "executed" and stored.edited is True
    assert stored.destination == "operator-2"


# ── destination integrity (anti-exfiltration) ───────────────────────────────


def test_destination_from_artifact_content_is_refused(tmp_path):
    gate = _gate(tmp_path)
    hostile = _artifact(meta={"pages": ["atlas"], "to": "attacker@evil.com"})
    with pytest.raises(DestinationIntegrityError):
        gate.propose(action_type="a", channel="draft-outbox", artifact=hostile, destination="op")
    assert _drafts(tmp_path) == [] and gate.store.list_pending() == []


def test_edit_cannot_smuggle_a_content_destination(tmp_path):
    gate = _gate(tmp_path)
    p = gate.propose(action_type="a", channel="draft-outbox", artifact=_artifact(), destination="op")
    # An edit that injects a destination-control field into the artifact is refused
    # at execution — nothing is delivered.
    with pytest.raises(DestinationIntegrityError):
        gate.approve(p.id, edited_artifact={"metadata": {"recipient": "evil@x.com"}})
    assert _drafts(tmp_path) == []


# ── the always-gated rule ───────────────────────────────────────────────────


def test_external_channel_never_auto_runs_even_with_the_flag(tmp_path):
    ext = _FakeExternalChannel()
    # The future escape hatch is ON — but it can only ever apply to INERT channels.
    gate = _gate(tmp_path, policy=ActionPolicy(auto_run_inert=True), external=ext)

    p = gate.propose(action_type="send", channel="ext-send", artifact=_artifact(), destination="op")
    assert p.status == "pending"          # gated despite the flag
    assert ext.sent == []                 # nothing was sent un-gated
    # It still requires an explicit approval to ever run.
    gate.approve(p.id)
    assert len(ext.sent) == 1


def test_inert_auto_run_only_when_flag_enabled(tmp_path):
    # Default policy (flag OFF): inert is gated too.
    gate_off = _gate(tmp_path / "off")
    p = gate_off.propose(action_type="a", channel="draft-outbox", artifact=_artifact(), destination="op")
    assert p.status == "pending" and _drafts(tmp_path / "off") == []

    # Flag ON: an inert channel may auto-run (this is the flag's only purpose).
    gate_on = _gate(tmp_path / "on", policy=ActionPolicy(auto_run_inert=True))
    p2 = gate_on.propose(action_type="a", channel="draft-outbox", artifact=_artifact(), destination="op")
    assert p2.status == "executed" and len(_drafts(tmp_path / "on")) == 1


def test_unknown_channel_fails_closed(tmp_path):
    gate = _gate(tmp_path)
    with pytest.raises(KeyError):
        gate.propose(action_type="a", channel="email", artifact=_artifact(), destination="op")


# ── audit ───────────────────────────────────────────────────────────────────


def test_every_outcome_is_audited_without_the_body(tmp_path):
    secret = "ZZ_SECRET_BODY_MARKER"
    gate = _gate(tmp_path)
    p = gate.propose(action_type="a", channel="draft-outbox",
                     artifact=_artifact(body=f"sensitive {secret}"), destination="op")
    gate.approve(p.id)
    q = gate.propose(action_type="a", channel="draft-outbox", artifact=_artifact(), destination="op")
    gate.reject(q.id, reason="no")

    records = []
    for f in [f for f in os.listdir(tmp_path) if f.startswith("runs-")]:
        records += [json.loads(line) for line in open(tmp_path / f, encoding="utf-8") if line.strip()]
    events = [r for r in records if r.get("type") == "action_event"]
    kinds = {r["event"] for r in events}
    assert {"proposed", "executed", "rejected"} <= kinds
    # The body is NEVER in the audit (only its length).
    assert secret not in json.dumps(records)
    proposed = next(r for r in events if r["event"] == "proposed")
    assert proposed["destination"] == "op" and proposed["channel"] == "draft-outbox"
    assert proposed["artifact_body_chars"] > 0 and "body" not in proposed


# ── CLI approvals surface ───────────────────────────────────────────────────


def test_cli_actions_list_approve_reject(tmp_path, monkeypatch, capsys):
    from mnesis_agents import cli

    gate = _gate(tmp_path)
    monkeypatch.setattr(cli, "_build_action_gate", lambda: gate)

    p = gate.propose(action_type="draft-brief", channel="draft-outbox",
                     artifact=_artifact(), destination="operator", rationale="why")

    assert cli.main(["actions"]) == 0
    out = capsys.readouterr().out
    assert p.id in out and "draft-outbox" in out and "pending" in out

    assert cli.main(["actions", "approve", p.id]) == 0
    assert "delivered" in capsys.readouterr().out  # the channel's DeliveryResult status
    assert len(_drafts(tmp_path)) == 1
    assert gate.store.get(p.id).status == "executed"  # the proposal's lifecycle status
