"""Tests for the Phase-2 confidence model (deterministic, injected clock)."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from mnesis import config
from mnesis.confidence import compute_confidence, resolve_decay_class
from mnesis.store import Page

NOW = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _page(days_old=0, source_count=1, contradicts=None, status="active", kind="fact",
          tags=None, decay_class=None) -> Page:
    return Page(
        id="p",
        title="t",
        last_confirmed=_iso(NOW - timedelta(days=days_old)),
        source_count=source_count,
        contradicts=contradicts or [],
        status=status,
        kind=kind,
        tags=tags or [],
        decay_class=decay_class,
    )


def test_fresh_single_source_fact_scores_moderate():
    score, bd = compute_confidence(_page(), now=NOW)
    # support .50 (1 source), retention ~1 (fresh) -> raw ~.75, no penalties.
    assert bd["support"] == 0.5
    assert bd["retention"] == 1.0
    assert 0.74 < score < 0.76


def test_adding_sources_raises_confidence():
    one, _ = compute_confidence(_page(source_count=1), now=NOW)
    three, bd = compute_confidence(_page(source_count=3), now=NOW)
    assert bd["support"] == 0.875
    assert three > one


def test_aging_one_stability_period_decays_retention():
    # fact stability = 180 days; at exactly S, retention = e^-1 ~ 0.368.
    _, bd = compute_confidence(_page(days_old=config.STABILITY_DAYS["fact"]), now=NOW)
    assert math.isclose(bd["retention"], math.exp(-1), abs_tol=1e-6)
    assert bd["retention"] < 0.5  # well below its fresh value of 1.0


def test_one_contradiction_multiplies_by_point_six():
    clean, _ = compute_confidence(_page(), now=NOW)
    conflicted, bd = compute_confidence(_page(contradicts=["other-page"]), now=NOW)
    assert bd["contradiction_factor"] == 0.6
    assert math.isclose(conflicted, clean * 0.6, abs_tol=1e-9)


def test_stale_page_capped():
    # A page that would otherwise score high, but is stale.
    score, bd = compute_confidence(_page(source_count=5, status="stale"), now=NOW)
    assert score == config.STALE_CAP == 0.40
    assert bd["stale_capped"] is True


def test_access_boost_applies_and_caps():
    base, _ = compute_confidence(_page(), now=NOW)
    boosted, bd = compute_confidence(_page(), access={"count": 3, "last_accessed": _iso(NOW)}, now=NOW)
    assert bd["access_boost"] == 0.06  # 0.02 * 3
    assert math.isclose(boosted, base + 0.06, abs_tol=1e-9)

    _, bd_cap = compute_confidence(_page(), access={"count": 100, "last_accessed": _iso(NOW)}, now=NOW)
    assert bd_cap["access_boost"] == config.ACCESS_BOOST_CAP == 0.10


def test_breakdown_derives_correctly():
    page = _page(days_old=90, source_count=2, contradicts=["x"])
    score, bd = compute_confidence(page, access={"count": 2, "last_accessed": _iso(NOW)}, now=NOW)
    # raw is the weighted blend of support and retention.
    expected_raw = (
        config.W_SUPPORT * bd["support"] + config.W_RETENTION * bd["retention"]
    ) / (config.W_SUPPORT + config.W_RETENTION)
    assert math.isclose(bd["raw"], expected_raw, abs_tol=1e-12)
    # score is raw * contradiction_factor + access_boost, clamped.
    expected = max(0.0, min(1.0, bd["raw"] * bd["contradiction_factor"] + bd["access_boost"]))
    assert math.isclose(score, expected, abs_tol=1e-12)
    assert 0.0 <= score <= 1.0


def test_resolve_decay_class():
    assert resolve_decay_class(_page(decay_class="transient")) == "transient"  # override wins
    assert resolve_decay_class(_page(tags=["decision:auth-migration"])) == "decision"
    assert resolve_decay_class(_page(tags=["bug:flaky-test"])) == "bug"
    assert resolve_decay_class(_page(tags=["project:atlas"])) == "fact"  # no class tag -> kind
    assert resolve_decay_class(_page(kind="digest")) == "fact"  # digest falls back to fact
    # A decision tag (slow) wins over a bug tag (fast).
    assert resolve_decay_class(_page(tags=["bug:x", "decision:y"])) == "decision"
