"""Confidence model (Phase 2) — derived, never hand-set.

Confidence is a value in ``[0, 1]`` computed from a page's Markdown inputs plus
an optional access boost from the durable state store. It is pure computation:
no I/O, no LLM. The formula (constants in :mod:`config`, so tuning needs no code
change) matches CLAUDE.md §8 "Confidence model":

    support   = 1 - 0.5 ** source_count                      # saturating support
    retention = exp(-days_since(last_confirmed) / S)          # Ebbinghaus decay
    contradiction_factor = 0.6 ** unresolved_contradictions   # one conflict ~0.6x
    access_boost = min(CAP, PER * recent_access_count)        # 0 if state lost

    raw  = (w_s * support + w_r * retention) / (w_s + w_r)
    conf = clamp(raw * contradiction_factor + access_boost, 0, 1)
    if status == "stale":  conf = min(conf, STALE_CAP)        # hard cap

``S`` is the stability (days) of the page's decay class (``config.STABILITY_DAYS``),
resolved from an explicit ``decay_class`` override, else its ``type:value`` tags,
else its ``kind``. Confidence degrades gracefully: with no access state it simply
computes from Markdown alone (``access_boost = 0``).
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from . import config
from .store import Page

# Slow/fast classes inferable from a page's type:value tags (a decision/
# architecture tag wins; bug/transient mean fast decay).
_TAG_CLASS_PRECEDENCE = ("decision", "architecture", "transient", "bug")


def resolve_decay_class(page: Page) -> str:
    """The decay class for ``page``: explicit override, else tags, else kind.

    Always returns a key present in ``config.STABILITY_DAYS`` (falling back to
    ``fact`` for kinds without their own stability, e.g. ``digest``).
    """
    if page.decay_class:
        return page.decay_class
    tag_types = {t.split(":", 1)[0] for t in page.tags if ":" in t}
    for cls in _TAG_CLASS_PRECEDENCE:
        if cls in tag_types:
            return cls
    if page.kind in config.STABILITY_DAYS:
        return page.kind
    return "fact"


def _days_since(timestamp: str, now: datetime) -> float:
    """Whole-and-fractional days from an ISO 8601 ``timestamp`` to ``now`` (>= 0)."""
    then = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    delta_days = (now - then).total_seconds() / 86400.0
    return max(0.0, delta_days)


def compute_confidence(
    page: Page,
    access: dict | None = None,
    now: datetime | None = None,
    apply_stale_cap: bool = True,
) -> tuple[float, dict]:
    """Compute ``(score, breakdown)`` for ``page``.

    ``access`` is the state-store record ``{"count", "last_accessed"}`` or
    ``None``. ``now`` is injectable for deterministic tests (defaults to UTC now).
    The ``breakdown`` exposes every term for explainability.

    ``apply_stale_cap`` (default True) clamps a ``stale`` page to ``STALE_CAP``.
    The lifecycle pass sets it False to read a page's *intrinsic* confidence when
    deciding whether to revive it — otherwise the cap would deadlock reactivation.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    decay_class = resolve_decay_class(page)
    stability = config.STABILITY_DAYS.get(decay_class, config.STABILITY_DAYS["fact"])

    days = _days_since(page.last_confirmed, now)
    support = 1.0 - 0.5 ** page.source_count
    retention = math.exp(-days / stability)

    unresolved = len(page.contradicts)
    contradiction_factor = 0.6 ** unresolved

    access_count = access["count"] if access else 0
    access_boost = min(config.ACCESS_BOOST_CAP, config.ACCESS_BOOST_PER * access_count)

    w_s, w_r = config.W_SUPPORT, config.W_RETENTION
    raw = (w_s * support + w_r * retention) / (w_s + w_r)
    conf = raw * contradiction_factor + access_boost
    conf = max(0.0, min(1.0, conf))

    stale_capped = False
    if apply_stale_cap and page.status == "stale" and conf > config.STALE_CAP:
        conf = config.STALE_CAP
        stale_capped = True

    breakdown = {
        "support": support,
        "retention": retention,
        "contradiction_factor": contradiction_factor,
        "access_boost": access_boost,
        "stale_capped": stale_capped,
        # Extra context for explainability (not part of the core five):
        "raw": raw,
        "decay_class": decay_class,
        "stability_days": stability,
        "days_since_confirmed": days,
        "unresolved_contradictions": unresolved,
        "score": conf,
    }
    return conf, breakdown
