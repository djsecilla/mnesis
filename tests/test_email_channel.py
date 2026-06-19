"""Tests for the EmailSendChannel (E2) — dry-run default, secret-scan, E1 gating,
and at-most-once with no auto-retry. Mock transport; no real network.
"""
from __future__ import annotations

from datetime import datetime, timezone

from mnesis_agents.channels import RISK_EXTERNAL, OutboundArtifact
from mnesis_agents.egress import EgressPolicy, EgressQuotaStore, Recipient
from mnesis_agents.email_channel import AmbiguousSendError, EmailSendChannel, _SentStore

_NOW = datetime(2026, 6, 19, 12, 0, 0, tzinfo=timezone.utc)
_RCPT = "ops@example.com"
_HOST, _PORT = "smtp.example.com", 587


def _egress(tmp_path, **kw) -> EgressPolicy:
    base = dict(
        enabled=True, recipient_allowlist=frozenset({_RCPT}),
        endpoint_allowlist=frozenset({f"{_HOST}:{_PORT}"}),
        rate_limit=10, daily_quota=10, global_rate_limit=20, global_daily_quota=50,
        quota_store=EgressQuotaStore(tmp_path / "egress.json"),
    )
    base.update(kw)
    return EgressPolicy(**base)


class _MockTransport:
    """Records sends; can be told to raise (ambiguous or clean)."""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.raises = raises

    def __call__(self, **kw):
        self.calls.append(kw)
        if self.raises is not None:
            raise self.raises


def _channel(tmp_path, *, dryrun, egress=None, transport=None, store="sent.json"):
    return EmailSendChannel(
        egress=egress or _egress(tmp_path), dryrun=dryrun,
        host=_HOST, port=_PORT, sender="mnesis@example.com",
        transport=transport, sent_store=_SentStore(tmp_path / store),
    )


def _artifact(body="Atlas uses Redis for caching. Coordinate with Sarah."):
    return OutboundArtifact(kind="brief", title="Atlas Brief", body=body)


def _ctx(**kw):
    base = dict(recipient_source="policy", proposal_id="p1", now=_NOW)
    base.update(kw)
    return base


# ── dry-run (default) ───────────────────────────────────────────────────────


def test_dry_run_renders_and_sends_nothing(tmp_path):
    transport = _MockTransport()
    ch = _channel(tmp_path, dryrun=True, transport=transport)
    res = ch.deliver(_artifact(), _RCPT, _ctx())

    assert res.status == "dry_run" and res.risk_class == RISK_EXTERNAL
    assert res.recipient == _RCPT and res.endpoint == f"{_HOST}:{_PORT}"
    assert res.content_hash and res.content_hash.startswith("sha256:")
    assert transport.calls == []                       # NOTHING was sent
    # The body is never in the result (only a content hash).
    assert "Atlas uses Redis" not in (res.detail + (res.error or ""))


def test_dry_run_is_the_default(tmp_path, monkeypatch):
    from mnesis_agents import config
    monkeypatch.setattr(config, "MNESIS_EMAIL_DRYRUN", True)
    ch = EmailSendChannel(egress=_egress(tmp_path), host=_HOST, port=_PORT,
                          sender="m@example.com", transport=_MockTransport(),
                          sent_store=_SentStore(tmp_path / "s.json"))
    assert ch.deliver(_artifact(), _RCPT, _ctx()).status == "dry_run"


# ── payload secret-scan ─────────────────────────────────────────────────────


def test_planted_secret_is_blocked(tmp_path):
    transport = _MockTransport()
    ch = _channel(tmp_path, dryrun=True, transport=transport)   # even in dry-run
    secret_body = "FYI the credentials: api_key=AKIAIOSFODNN7EXAMPLE — please rotate."
    res = ch.deliver(_artifact(body=secret_body), _RCPT, _ctx())

    assert res.status == "blocked" and "secret-scan" in res.detail
    assert transport.calls == []                       # blocked, not sent
    # The secret value itself is never echoed back.
    assert "AKIAIOSFODNN7EXAMPLE" not in (res.detail + (res.error or ""))


def test_secret_scan_blocks_a_live_send_too(tmp_path):
    transport = _MockTransport()
    ch = _channel(tmp_path, dryrun=False, transport=transport)
    res = ch.deliver(_artifact(body="token: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"), _RCPT, _ctx())
    assert res.status == "blocked" and transport.calls == []


# ── E1 gating ───────────────────────────────────────────────────────────────


def test_egress_denied_prevents_send(tmp_path):
    transport = _MockTransport()
    # Live mode, but the recipient is not allowlisted → E1 denies.
    ch = _channel(tmp_path, dryrun=False, transport=transport)
    res = ch.deliver(_artifact(), "stranger@elsewhere.com", _ctx())
    assert res.status == "blocked" and "egress" in res.detail.lower()
    assert transport.calls == []


def test_egress_disabled_blocks_live_send(tmp_path):
    transport = _MockTransport()
    ch = _channel(tmp_path, dryrun=False, egress=_egress(tmp_path, enabled=False), transport=transport)
    assert ch.deliver(_artifact(), _RCPT, _ctx()).status == "blocked"
    assert transport.calls == []


def test_content_sourced_recipient_blocked_live(tmp_path):
    transport = _MockTransport()
    ch = _channel(tmp_path, dryrun=False, transport=transport)
    # Allowlisted address, but sourced from content → E1 rejects.
    res = ch.deliver(_artifact(), _RCPT, _ctx(recipient_source="content"))
    assert res.status == "blocked" and transport.calls == []


# ── live send + at-most-once ────────────────────────────────────────────────


def test_live_send_happens_exactly_once(tmp_path):
    transport = _MockTransport()
    ch = _channel(tmp_path, dryrun=False, transport=transport)
    res = ch.deliver(_artifact(), _RCPT, _ctx(proposal_id="px"))
    assert res.status == "sent" and len(transport.calls) == 1
    # The transport got the rendered message + recipient (never logged here).
    assert transport.calls[0]["recipient"] == _RCPT


def test_repeat_with_same_idempotency_key_does_not_resend(tmp_path):
    transport = _MockTransport()
    ch = _channel(tmp_path, dryrun=False, transport=transport)
    a = ch.deliver(_artifact(), _RCPT, _ctx(proposal_id="dup"))
    b = ch.deliver(_artifact(), _RCPT, _ctx(proposal_id="dup"))
    assert a.status == "sent" and b.status == "sent"
    assert len(transport.calls) == 1                   # at-most-once


def test_ambiguous_failure_is_needs_human_and_not_retried(tmp_path):
    transport = _MockTransport(raises=AmbiguousSendError("connection dropped after DATA"))
    ch = _channel(tmp_path, dryrun=False, transport=transport)
    res = ch.deliver(_artifact(), _RCPT, _ctx(proposal_id="amb"))
    assert res.status == "needs_human" and "NOT auto-retried" in res.detail
    assert len(transport.calls) == 1
    # A repeat does NOT re-send — the unresolved attempt stays needs_human.
    again = ch.deliver(_artifact(), _RCPT, _ctx(proposal_id="amb"))
    assert again.status == "needs_human" and len(transport.calls) == 1


def test_clean_failure_reports_failed_without_retry(tmp_path):
    transport = _MockTransport(raises=RuntimeError("connection refused"))
    ch = _channel(tmp_path, dryrun=False, transport=transport)
    res = ch.deliver(_artifact(), _RCPT, _ctx(proposal_id="cf"))
    assert res.status == "failed" and len(transport.calls) == 1


# ── TLS required ────────────────────────────────────────────────────────────


def test_live_send_refused_without_tls(tmp_path):
    transport = _MockTransport()
    ch = EmailSendChannel(
        egress=_egress(tmp_path), dryrun=False, host=_HOST, port=_PORT,
        sender="m@example.com", starttls=False, transport=transport,
        sent_store=_SentStore(tmp_path / "s.json"),
    )
    res = ch.deliver(_artifact(), _RCPT, _ctx())
    assert res.status == "blocked" and "tls" in res.detail.lower()
    assert transport.calls == []
