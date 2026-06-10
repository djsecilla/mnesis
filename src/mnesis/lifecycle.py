"""Decay / lifecycle pass (Phase 2): let knowledge fade and recover gracefully.

``recompute_all`` recomputes every page's confidence (refreshing the cached value
in the search index) and transitions pages between ``active`` and ``stale``:

  - **active -> stale** when confidence falls below ``STALE_THRESHOLD`` *and* the
    page has been inactive — no access and no reinforcement — for longer than its
    decay class's ``INACTIVITY_DAYS`` window. Two clocks combine here: the most
    recent of ``last_confirmed`` (reinforcement) or ``last_accessed`` (a read)
    defers staleness, so an often-read or freshly-confirmed page stays active.
  - **stale -> active** only on **reinforcement** (a fresh ``last_confirmed``),
    never on a read alone, and never for a page explicitly ``superseded_by``
    another. Access boosts confidence but cannot, by itself, revive a stale page.

Stale means demoted, never deleted — the Markdown and git history are preserved.
Each status change is one commit (``mnesis: <id> -> stale|active``); a run with no
time change makes no commits (idempotent). The scheduler that fires this on a
cadence is Phase 4 — here it is the `mnesis decay` command.
"""

from __future__ import annotations

from datetime import datetime, timezone

from . import config, confidence, search, state, store
from .store import Page


def _age_days(timestamp: str | None, now: datetime) -> float:
    """Days from an ISO 8601 ``timestamp`` to ``now`` (>= 0); ``inf`` if missing."""
    if not timestamp:
        return float("inf")
    then = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return max(0.0, (now - then).total_seconds() / 86400.0)


def _next_status(page: Page, conf: float, access: dict | None, now: datetime) -> str:
    """The status ``page`` should hold given its confidence and activity."""
    cls = confidence.resolve_decay_class(page)
    window = config.INACTIVITY_DAYS.get(cls, config.INACTIVITY_DAYS["fact"])

    reinforced_age = _age_days(page.last_confirmed, now)
    accessed_age = _age_days(access.get("last_accessed") if access else None, now)
    # Inactivity = time since the most recent touch of any kind.
    inactivity = min(reinforced_age, accessed_age)

    if page.status == "active":
        if conf < config.STALE_THRESHOLD and inactivity > window:
            return "stale"
        return "active"

    # Stale: revive only on recent *reinforcement* (not a read), and never if the
    # page was explicitly superseded by another.
    if (
        page.superseded_by is None
        and conf >= config.STALE_THRESHOLD
        and reinforced_age <= window
    ):
        return "active"
    return "stale"


def recompute_all(now: datetime | None = None) -> dict:
    """Recompute confidence corpus-wide and apply active<->stale transitions.

    Returns ``{scanned, restaled, reactivated, unchanged}``. ``now`` is injectable
    for deterministic tests. Idempotent: a second run with no time change makes no
    status changes and therefore no commits.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    summary = {"scanned": 0, "restaled": 0, "reactivated": 0, "unchanged": 0}
    for page in store.list_pages():
        summary["scanned"] += 1
        access = state.get_access(page.id)
        # Use the page's *intrinsic* confidence (ignore the stale cap) for the
        # transition decision, so a recovered stale page can cross the threshold.
        conf, _ = confidence.compute_confidence(
            page, access=access, now=now, apply_stale_cap=False
        )
        new_status = _next_status(page, conf, access, now)

        if new_status != page.status:
            page.status = new_status
            store.write_page(page, message=f"mnesis: {page.id} -> {new_status}")
            summary["restaled" if new_status == "stale" else "reactivated"] += 1
        else:
            summary["unchanged"] += 1

        # Refresh the cached confidence (and status) in the rebuildable index.
        # This is a cache update, not a Markdown write — no commit.
        search.upsert(page)

    return summary
