"""Tests for the OutboundChannel pattern + the inert safe channels (A1).

Offline, temp dirs. Validates that DraftOutboxChannel writes a draft file (never
sends), LocalNotifyChannel records a local-operator notification, both report
``risk_class=inert``, the interface defaults to the gated ``external`` class, and
the registry resolves channels by name.
"""
from __future__ import annotations

import json

from mnesis_agents.channels import (
    RISK_EXTERNAL,
    RISK_INERT,
    ChannelRegistry,
    DeliveryResult,
    DraftOutboxChannel,
    LocalNotifyChannel,
    OutboundArtifact,
    OutboundChannel,
    default_channel_registry,
)


def _artifact():
    return OutboundArtifact(
        kind="brief", title="Atlas Redis Brief",
        body="Atlas uses Redis for caching. Coordinate with Sarah.",
        metadata={"pages": ["atlas"]},
    )


# ── interface contract ──────────────────────────────────────────────────────


def test_interface_defaults_to_gated_external_risk_class():
    # A channel that forgets to declare a risk class is treated as the RISKY one.
    assert OutboundChannel.risk_class == RISK_EXTERNAL


def test_safe_channels_declare_inert():
    assert DraftOutboxChannel.risk_class == RISK_INERT
    assert LocalNotifyChannel.risk_class == RISK_INERT
    assert DraftOutboxChannel().name == "draft-outbox"
    assert LocalNotifyChannel().name == "local-notify"


# ── DraftOutboxChannel (inert) ──────────────────────────────────────────────


def test_draft_outbox_writes_a_draft_and_returns_its_path(tmp_path):
    outbox = tmp_path / "outbox"
    ch = DraftOutboxChannel(outbox)
    res = ch.deliver(_artifact(), destination="operator", context={"run": "r1"})

    assert isinstance(res, DeliveryResult)
    assert res.ok and res.status == "delivered" and res.risk_class == RISK_INERT
    assert res.channel == "draft-outbox" and res.destination == "operator"

    path = res.location
    assert path is not None and path.endswith(".md")
    from pathlib import Path
    p = Path(path)
    assert p.is_file() and p.parent == outbox
    text = p.read_text(encoding="utf-8")
    # The draft carries metadata + the body, marked as a non-sent draft.
    assert "channel: draft-outbox" in text and "risk_class: inert" in text
    assert "status: draft" in text and "destination: operator" in text
    assert "Atlas uses Redis for caching." in text
    # Only the one draft was written — nothing was "sent".
    assert list(outbox.glob("*.md")) == [p]


def test_draft_outbox_failure_is_reported_not_raised(tmp_path):
    # Point the outbox at a path that cannot be a directory (a regular file).
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    res = DraftOutboxChannel(blocker / "sub").deliver(_artifact())
    assert not res.ok and res.status == "failed" and res.error


# ── LocalNotifyChannel (inert) ──────────────────────────────────────────────


def test_local_notify_records_a_local_notification(tmp_path):
    notify = tmp_path / "notifications.jsonl"
    res = LocalNotifyChannel(notify).deliver(_artifact(), destination="operator")

    assert res.ok and res.risk_class == RISK_INERT and res.channel == "local-notify"
    assert res.location == str(notify)
    lines = notify.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["channel"] == "local-notify" and rec["destination"] == "operator"
    assert rec["title"] == "Atlas Redis Brief" and rec["risk_class"] == "inert"


def test_local_notify_appends(tmp_path):
    notify = tmp_path / "n.jsonl"
    ch = LocalNotifyChannel(notify)
    ch.deliver(_artifact(), destination="operator")
    ch.deliver(_artifact(), destination="operator")
    assert len(notify.read_text(encoding="utf-8").strip().splitlines()) == 2


# ── registry ────────────────────────────────────────────────────────────────


def test_registry_resolves_channels_by_name(tmp_path):
    reg = ChannelRegistry([DraftOutboxChannel(tmp_path / "out"), LocalNotifyChannel(tmp_path / "n.jsonl")])
    assert reg.names() == ["draft-outbox", "local-notify"]
    assert "draft-outbox" in reg and "email" not in reg
    assert isinstance(reg.get("draft-outbox"), DraftOutboxChannel)
    assert reg.risk_class("local-notify") == RISK_INERT


def test_registry_deliver_routes_to_the_named_channel(tmp_path):
    reg = ChannelRegistry([DraftOutboxChannel(tmp_path / "out")])
    res = reg.deliver("draft-outbox", _artifact(), destination="operator")
    assert res.ok and res.channel == "draft-outbox"


def test_registry_unknown_channel_raises_keyerror(tmp_path):
    reg = ChannelRegistry([DraftOutboxChannel(tmp_path / "out")])
    import pytest
    with pytest.raises(KeyError):
        reg.get("email")


def test_default_registry_has_only_inert_channels():
    reg = default_channel_registry()
    assert set(reg.names()) == {"draft-outbox", "local-notify"}
    # No external-send channel ships in this set.
    assert all(reg.risk_class(n) == RISK_INERT for n in reg.names())
