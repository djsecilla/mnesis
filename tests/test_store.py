"""Tests for the canonical Markdown + git store.

Each test runs against an isolated temporary git repo (so the project's own
history is never touched), with config paths monkeypatched onto the tmp tree.
"""

from __future__ import annotations

import subprocess

import pytest

from llmwiki import config, store
from llmwiki.store import Page


@pytest.fixture()
def wiki(tmp_path, monkeypatch):
    """Point the store at a fresh tmp git repo and yield path helpers."""
    root = tmp_path / "wiki"
    pages = root / "pages"
    pages.mkdir(parents=True)
    monkeypatch.setattr(config, "WIKI_ROOT", root)
    monkeypatch.setattr(config, "PAGES_DIR", pages)

    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@localhost"], check=True
    )
    return tmp_path


def _commit_count(repo: object) -> int:
    out = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--count", "HEAD"],
        capture_output=True,
        text=True,
    )
    return int(out.stdout.strip()) if out.returncode == 0 else 0


def test_create_read_roundtrip(wiki):
    page = Page(
        id=store.make_id("Project Atlas uses Redis for caching"),
        title="Project Atlas uses Redis for caching",
        body="Project Atlas uses Redis as its primary caching layer.",
        sources=["atlas-architecture-notes"],
        tags=["project:atlas", "library:redis", "concept:caching"],
    )
    store.write_page(page)

    got = store.read_page(page.id)
    assert got == page  # identical round-trip, including the refreshed `updated`


def test_make_id_collision_safe(wiki):
    p1 = Page(id=store.make_id("Same Title"), title="Same Title", body="a")
    store.write_page(p1)
    p2 = Page(id=store.make_id("Same Title"), title="Same Title", body="b")
    store.write_page(p2)
    assert p1.id == "same-title"
    assert p2.id == "same-title-2"


def test_list_pages_and_filters(wiki):
    fact = Page(id="f", title="A fact", body="x", kind="fact")
    digest = Page(id="d", title="A digest", body="y", kind="digest", question="Q?")
    store.write_page(fact)
    store.write_page(digest)

    ids = [p.id for p in store.list_pages()]
    assert ids == ["d", "f"]
    assert [p.id for p in store.list_pages(kind="fact")] == ["f"]
    assert [p.id for p in store.list_pages(kind="digest")] == ["d"]
    assert [p.id for p in store.list_pages(status="active")] == ["d", "f"]


def test_write_commits_one_per_write(wiki):
    assert _commit_count(wiki) == 0
    page = Page(id="p", title="Title", body="first")
    store.write_page(page)
    assert _commit_count(wiki) == 1

    first_updated = page.updated
    page.body = "second"
    store.write_page(page)
    assert _commit_count(wiki) == 2
    assert page.updated > first_updated  # `updated` bumped

    reread = store.read_page("p")
    assert reread.body == "second"


def test_supersede_links_both_directions(wiki):
    old = Page(id="old-claim", title="Old claim", body="outdated")
    store.write_page(old)
    before = _commit_count(wiki)

    new = Page(id=store.make_id("New claim"), title="New claim", body="current")
    store.supersede("old-claim", new)

    # Exactly one commit for the whole supersede operation.
    assert _commit_count(wiki) == before + 1

    old_reread = store.read_page("old-claim")
    new_reread = store.read_page(new.id)
    assert old_reread.status == "stale"
    assert old_reread.superseded_by == new.id
    assert new_reread.supersedes == "old-claim"
    assert new_reread.status == "active"
