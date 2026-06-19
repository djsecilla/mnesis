"""Tests for the egress control plane (E1) — default-deny, recipient-source rule,
allowlists, quotas/rate, and the kill-switch. Offline, deterministic (injected
clock + a temp quota ledger).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from mnesis_agents.egress import (
    EgressPolicy,
    EgressQuotaStore,
    Recipient,
    TRUSTED_SOURCES,
    validate_recipient,
)

_NOW = datetime(2026, 6, 19, 12, 0, 0, tzinfo=timezone.utc)
_EP = "smtp.example.com:587"
_ALLOW = frozenset({"ops@example.com"})
_EPS = frozenset({"smtp.example.com:587"})


def _policy(tmp_path, **kw) -> EgressPolicy:
    kw.setdefault("quota_store", EgressQuotaStore(tmp_path / "egress.json"))
    return EgressPolicy(**kw)


def _enabled(tmp_path, **kw) -> EgressPolicy:
    base = dict(enabled=True, recipient_allowlist=_ALLOW, endpoint_allowlist=_EPS,
                rate_limit=10, daily_quota=50, global_rate_limit=20, global_daily_quota=100)
    base.update(kw)
    return _policy(tmp_path, **base)


def _ok(tmp_path, policy, *, recipient=None, endpoint=_EP, risk="external", now=_NOW):
    recipient = recipient or Recipient("ops@example.com", "policy")
    return policy.check_send_allowed(risk, recipient, endpoint, now=now)


# ── default-deny ────────────────────────────────────────────────────────────


def test_default_policy_denies_everything(tmp_path):
    # An EgressPolicy with no config (disabled) refuses even a perfect request.
    p = _policy(tmp_path)  # enabled defaults to False
    d = _ok(tmp_path, EgressPolicy(enabled=False, recipient_allowlist=_ALLOW,
                                   endpoint_allowlist=_EPS, quota_store=p.quota_store))
    assert d.denied and "disabled" in d.reason


def test_egress_disabled_denies_every_send(tmp_path):
    p = _policy(tmp_path, enabled=False, recipient_allowlist=_ALLOW, endpoint_allowlist=_EPS)
    assert _ok(tmp_path, p).denied


# ── recipient: source rule + allowlist ──────────────────────────────────────


def test_allowlisted_policy_sourced_recipient_is_allowed(tmp_path):
    p = _enabled(tmp_path)
    d = _ok(tmp_path, p, recipient=Recipient("ops@example.com", "policy"))
    assert d.allowed and "permitted" in d.reason
    # A user-sourced recipient is equally trusted.
    assert _ok(tmp_path, p, recipient=Recipient("ops@example.com", "user")).allowed


def test_non_allowlisted_recipient_is_denied(tmp_path):
    p = _enabled(tmp_path)
    d = _ok(tmp_path, p, recipient=Recipient("stranger@elsewhere.com", "policy"))
    assert d.denied and "allowlist" in d.reason


def test_content_sourced_recipient_denied_even_if_allowlisted(tmp_path):
    # The exact allowlisted address — but sourced from CONTENT → rejected outright.
    p = _enabled(tmp_path)
    d = _ok(tmp_path, p, recipient=Recipient("ops@example.com", "content"))
    assert d.denied and "not policy/user" in d.reason


def test_unknown_source_fails_closed(tmp_path):
    p = _enabled(tmp_path)
    # A bare string with no declared source → unknown → denied.
    d = p.check_send_allowed("external", "ops@example.com", _EP, now=_NOW)
    assert d.denied and "not policy/user" in d.reason
    for bad in ("model", "artifact", "", "system"):
        assert validate_recipient(Recipient("ops@example.com", bad), policy=p).denied


def test_validate_recipient_source_set():
    assert TRUSTED_SOURCES == frozenset({"policy", "user"})


def test_domain_allowlist_entry_matches(tmp_path):
    p = _enabled(tmp_path, recipient_allowlist=frozenset({"@example.com"}))
    assert _ok(tmp_path, p, recipient=Recipient("anyone@example.com", "policy")).allowed
    assert _ok(tmp_path, p, recipient=Recipient("anyone@other.com", "policy")).denied


# ── endpoint allowlist ──────────────────────────────────────────────────────


def test_non_allowlisted_endpoint_is_denied(tmp_path):
    p = _enabled(tmp_path)
    d = _ok(tmp_path, p, endpoint="smtp.evil.com:25")
    assert d.denied and "endpoint" in d.reason
    # An empty endpoint allowlist denies everything.
    p2 = _enabled(tmp_path, endpoint_allowlist=frozenset())
    assert _ok(tmp_path, p2).denied


# ── kill-switch ─────────────────────────────────────────────────────────────


def test_kill_switch_denies_all(tmp_path):
    p = _enabled(tmp_path, kill=True)
    d = _ok(tmp_path, p)
    assert d.denied and "kill-switch" in d.reason


# ── quotas / rate ───────────────────────────────────────────────────────────


def test_daily_quota_denies_when_exceeded(tmp_path):
    p = _enabled(tmp_path, daily_quota=2)
    rcpt = Recipient("ops@example.com", "policy")
    assert _ok(tmp_path, p).allowed
    p.record_send(rcpt, now=_NOW)
    p.record_send(rcpt, now=_NOW)
    d = _ok(tmp_path, p)
    assert d.denied and "daily quota" in d.reason


def test_rate_limit_denies_within_window(tmp_path):
    p = _enabled(tmp_path, rate_limit=2, rate_window_seconds=3600, daily_quota=100)
    rcpt = Recipient("ops@example.com", "policy")
    p.record_send(rcpt, now=_NOW)
    p.record_send(rcpt, now=_NOW)
    assert _ok(tmp_path, p).denied  # 2/2 within the window
    # …but once the window has passed, sending is allowed again.
    later = _NOW + timedelta(seconds=3601)
    assert _ok(tmp_path, p, now=later).allowed


def test_global_quota_denies_across_recipients(tmp_path):
    p = _enabled(tmp_path, global_daily_quota=1, daily_quota=100)
    p.record_send(Recipient("ops@example.com", "policy"), now=_NOW)
    # A different recipient still hits the GLOBAL cap.
    d = _ok(tmp_path, p, recipient=Recipient("ops@example.com", "policy"))
    assert d.denied and "global daily quota" in d.reason


def test_zero_limit_denies(tmp_path):
    p = _enabled(tmp_path, rate_limit=0)
    d = _ok(tmp_path, p)
    assert d.denied and "zero" in d.reason


# ── risk + fail-closed ──────────────────────────────────────────────────────


def test_only_external_risk_is_governed(tmp_path):
    p = _enabled(tmp_path)
    d = _ok(tmp_path, p, risk="inert")
    assert d.denied and "external sends only" in d.reason


def test_decision_masks_the_recipient(tmp_path):
    p = _enabled(tmp_path)
    d = _ok(tmp_path, p)
    # The decision carries a MASKED recipient — never the raw address.
    assert d.recipient == "o***@example.com"


def test_check_is_read_only_until_record(tmp_path):
    # check_send_allowed never consumes quota; only record_send does.
    p = _enabled(tmp_path, daily_quota=1)
    for _ in range(5):
        assert _ok(tmp_path, p).allowed
    p.record_send(Recipient("ops@example.com", "policy"), now=_NOW)
    assert _ok(tmp_path, p).denied
