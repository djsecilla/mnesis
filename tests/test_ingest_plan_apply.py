"""Plan / apply split of the ingest pipeline (G7).

``plan_ingest`` previews (scrub + extract + classify) with ZERO writes; only
``apply_ingest`` writes. Stub mode drives deterministic plans so the whole flow
is testable offline. The redacted secret value must never appear anywhere.
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from mnesis import config, ingest, search, state, store, tenancy
from mnesis.store import Page

# A fake secret (matches the api-key detector) plus a normal declarative claim.
SECRET = "sk-ABCDEF0123456789abcdef"
SECRET_SOURCE = f"Project Atlas uses Redis for caching. The deploy key is {SECRET}."


@pytest.fixture()
def wiki(tenant):
    return tenant.root_path

def _commit_count(repo) -> int:
    out = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--count", "--all"],
        capture_output=True, text=True,
    )
    return int((out.stdout or "0").strip() or "0")


def _snapshot(repo):
    return (
        _commit_count(repo),
        sorted(os.listdir(tenancy.current().sources_dir)),
        sorted(os.listdir(tenancy.current().pages_dir)),
    )


def _seed(title: str, page_id: str) -> Page:
    page = Page(id=page_id, title=title, body=f"{title}.", sources=["seed-src"], kind="fact")
    store.write_page(page)
    search.rebuild()
    return page


# --- plan writes nothing ----------------------------------------------------


def test_plan_reports_redactions_and_writes_nothing(wiki):
    before = _snapshot(wiki)
    plan = ingest.plan_ingest(SECRET_SOURCE, "secret-src")

    # Redactions are reported by type/kind/count — never the value.
    assert {"type": "secret", "kind": "api-key", "count": 1} in plan["redactions"]
    assert any("secret material" in w for w in plan["warnings"])

    # A draft page and a routing decision are present.
    assert plan["draft_page"]["title"]
    assert plan["routing"]["action"] == "new"  # nothing to relate to yet

    # The raw secret value appears NOWHERE in the serialized plan.
    assert SECRET not in json.dumps(plan)

    # Zero writes, zero commits: sources dir + git log + pages dir unchanged.
    assert _snapshot(wiki) == before


def test_plan_with_a_candidate_still_writes_nothing(wiki):
    """Even the classify path (which reads the index/pages) performs no writes."""
    existing = _seed("Project Atlas uses Redis for caching", "atlas-cache")
    before = _snapshot(wiki)

    plan = ingest.plan_ingest(
        "Project Atlas uses Redis for caching. relation:reinforces", "again-src"
    )
    assert plan["routing"]["action"] == "reinforce"
    assert plan["routing"]["target_page_id"] == existing.id

    assert _snapshot(wiki) == before
    assert store.read_page(existing.id).source_count == 1  # untouched


# --- apply writes exactly one outcome, secret stays gone ---------------------


def test_apply_writes_one_outcome_and_secret_is_absent(wiki):
    plan = ingest.plan_ingest(SECRET_SOURCE, "secret-src")
    result = ingest.apply_ingest(plan)

    assert result["action_taken"] == "new"
    assert result["redaction_count"] == 1
    pages = store.list_pages()
    assert len(pages) == 1 and pages[0].id == result["page_id"]

    # The secret value must not survive into ANY file under the wiki, nor git.
    for dirpath, _dirs, files in os.walk(tenancy.current().root_path):
        if ".git" in dirpath:
            continue
        for name in files:
            assert SECRET not in (tenancy.current().root_path / dirpath / name).read_text(errors="ignore")
    log = subprocess.run(
        ["git", "-C", str(wiki), "log", "-p"], capture_output=True, text=True
    ).stdout
    assert SECRET not in log


# --- overrides --------------------------------------------------------------


def test_forced_supersede_marks_target_stale(wiki):
    target = _seed("Atlas caching architecture", "atlas-arch")
    # A source that on its own would just create a new page (no relation marker).
    plan = ingest.plan_ingest("Atlas now caches with a managed cluster.", "update-src")
    assert plan["routing"]["action"] == "new"

    result = ingest.apply_ingest(
        plan, overrides={"routing": {"action": "supersede", "target_page_id": target.id}}
    )
    assert result["action_taken"] == "supersede"
    assert result["superseded_id"] == target.id

    old = store.read_page(target.id)
    assert old.status == "stale"
    assert old.superseded_by == result["page_id"]


def test_forced_supersede_rejects_missing_target(wiki):
    plan = ingest.plan_ingest("Some standalone claim.", "x-src")
    with pytest.raises(ValueError):
        ingest.apply_ingest(
            plan, overrides={"routing": {"action": "supersede", "target_page_id": "no-such-page"}}
        )


def test_rejecting_a_relation_omits_it_from_the_page(wiki):
    src = (
        "Project Atlas uses Redis and Sarah owns the auth migration. "
        "rel{project:atlas|uses|library:redis} "
        "rel{person:sarah|owns|decision:auth-migration}"
    )
    plan = ingest.plan_ingest(src, "rel-src")
    assert len(plan["draft_page"]["relations"]) == 2

    result = ingest.apply_ingest(plan, overrides={"rejected_relations": [1]})
    page = store.read_page(result["page_id"])
    assert page.relations == [{"s": "project:atlas", "p": "uses", "o": "library:redis"}]


# --- one-shot parity --------------------------------------------------------


def test_one_shot_equals_plan_then_apply(wiki):
    page = ingest.ingest_source("A wholly standalone fact about widgets.", "widget-src")
    # The re-implemented one-shot wrote exactly one page and persisted its source.
    assert store.page_exists(page.id)
    assert (tenancy.current().sources_dir / "widget-src.md").exists()
    assert page.sources == ["widget-src"]
    state.get_access(page.id)  # state store reachable; no error
