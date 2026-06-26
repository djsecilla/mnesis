"""Tests for the decay/lifecycle pass (injected clock)."""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from mnesis import config, lifecycle, search, state, store
from mnesis.store import Page

NOW = datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@pytest.fixture()
def wiki(tenant, monkeypatch):
    monkeypatch.setattr(config, "STALE_THRESHOLD", 0.5)
    return tenant.root_path

def _commit_count(repo) -> int:
    out = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--count", "HEAD"], capture_output=True, text=True
    )
    return int(out.stdout.strip()) if out.returncode == 0 else 0


def _aged_page(page_id: str, days_old: int) -> Page:
    page = Page(
        id=page_id,
        title=f"Claim {page_id}",
        body=f"A claim {page_id} about the project.",
        sources=["s1"],
        source_count=1,
        last_confirmed=_iso(NOW - timedelta(days=days_old)),
    )
    store.write_page(page)
    return page


def test_aged_unaccessed_low_page_becomes_stale(wiki):
    _aged_page("p", days_old=180)  # > fact inactivity window (90), conf ~0.43 < 0.5

    summary = lifecycle.recompute_all(now=NOW)

    assert summary["restaled"] == 1
    assert store.read_page("p").status == "stale"


def test_recently_accessed_page_stays_active(wiki):
    _aged_page("p", days_old=180)
    state.record_access("p")  # a read defers staleness via the inactivity clock

    summary = lifecycle.recompute_all(now=NOW)

    assert summary["restaled"] == 0
    assert summary["unchanged"] == 1
    assert store.read_page("p").status == "active"


def test_reinforcement_reactivates_stale_page(wiki):
    page = _aged_page("p", days_old=180)
    lifecycle.recompute_all(now=NOW)
    assert store.read_page("p").status == "stale"

    # Reinforce: a new source resets the retention clock and raises support.
    page = store.read_page("p")
    page.sources.append("s2")
    page.source_count = 2
    page.last_confirmed = _iso(NOW)
    store.write_page(page, message="mnesis: reinforce p")

    summary = lifecycle.recompute_all(now=NOW)
    assert summary["reactivated"] == 1
    assert store.read_page("p").status == "active"


def test_read_alone_does_not_reactivate_stale_page(wiki):
    _aged_page("p", days_old=180)
    lifecycle.recompute_all(now=NOW)
    assert store.read_page("p").status == "stale"

    # A read (access) boosts confidence but must NOT revive a stale page.
    for _ in range(10):
        state.record_access("p")
    summary = lifecycle.recompute_all(now=NOW)
    assert summary["reactivated"] == 0
    assert store.read_page("p").status == "stale"


def test_superseded_page_not_reactivated(wiki):
    page = _aged_page("p", days_old=180)
    page.status = "stale"
    page.superseded_by = "newer-page"
    page.source_count = 3
    page.last_confirmed = _iso(NOW)  # fresh + high support, but explicitly superseded
    store.write_page(page)

    summary = lifecycle.recompute_all(now=NOW)
    assert summary["reactivated"] == 0
    assert store.read_page("p").status == "stale"


def test_second_run_is_a_noop(wiki):
    _aged_page("p", days_old=180)
    _aged_page("q", days_old=10)  # fresh, stays active

    lifecycle.recompute_all(now=NOW)  # p -> stale (one commit)
    count_after_first = _commit_count(wiki)

    summary = lifecycle.recompute_all(now=NOW)  # no time change -> no transitions
    assert summary["restaled"] == 0
    assert summary["reactivated"] == 0
    assert _commit_count(wiki) == count_after_first  # no new commits
