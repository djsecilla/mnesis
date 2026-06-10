"""Tests for the ingestion pipeline (offline stub mode).

Runs entirely with the LLM stub: no API key, no network. An isolated tmp git
repo keeps the project history clean.
"""

from __future__ import annotations

import subprocess

import pytest

from mnesis import config, ingest, store

FAKE_SECRET = "sk-test1234567890ABCDEFGHijklmnop"


@pytest.fixture()
def wiki(tmp_path, monkeypatch):
    """Isolated wiki tree + tmp git repo, with the LLM forced into stub mode."""
    root = tmp_path / "wiki"
    (root / "pages").mkdir(parents=True)
    (root / "sources").mkdir(parents=True)
    monkeypatch.setattr(config, "WIKI_ROOT", root)
    monkeypatch.setattr(config, "PAGES_DIR", root / "pages")
    monkeypatch.setattr(config, "SOURCES_DIR", root / "sources")
    monkeypatch.setattr(config, "INDEX_DIR", root / ".index")
    monkeypatch.setattr(config, "WIKI_LLM_STUB", True)

    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@localhost"], check=True
    )
    return tmp_path


def _commit_count(repo) -> int:
    out = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--count", "HEAD"],
        capture_output=True,
        text=True,
    )
    return int(out.stdout.strip()) if out.returncode == 0 else 0


def test_ingest_redacts_secret_and_writes_page(wiki):
    raw = (
        "Project Atlas uses Redis for caching. "
        f"The deploy key is {FAKE_SECRET} and must be rotated quarterly."
    )
    page = ingest.ingest_source(raw, "atlas-notes")

    # A fact page was created and is readable back.
    assert page.kind == "fact"
    assert page.sources == ["atlas-notes"]
    assert page.source_count == 1
    reread = store.read_page(page.id)
    assert reread == page

    # The secret is absent from the PAGE (frontmatter + body).
    page_text = (config.PAGES_DIR / f"{page.id}.md").read_text()
    assert FAKE_SECRET not in page_text

    # The secret is absent from the SAVED SOURCE.
    source_text = (config.SOURCES_DIR / "atlas-notes.md").read_text()
    assert FAKE_SECRET not in source_text
    assert "[REDACTED:SECRET]" in source_text  # it was caught, not just missing


def test_frontmatter_is_well_formed(wiki):
    page = ingest.ingest_source("Sarah owns the auth migration for Atlas.", "auth-note")
    reread = store.read_page(page.id)
    # Required schema fields are present and sane.
    assert reread.id and reread.title
    assert reread.created and reread.updated and reread.last_confirmed
    assert reread.status == "active"
    assert reread.kind == "fact"
    assert reread.supersedes is None and reread.superseded_by is None


def test_ingest_creates_git_commits(wiki):
    assert _commit_count(wiki) == 0
    ingest.ingest_source("Atlas depends on the Redis cache.", "dep-note")
    # One commit for the persisted source, one for the page.
    assert _commit_count(wiki) == 2

    msgs = subprocess.run(
        ["git", "-C", str(wiki), "log", "--pretty=%s"],
        capture_output=True,
        text=True,
    ).stdout
    assert "mnesis: source dep-note" in msgs
    assert "mnesis: write" in msgs


def test_fallback_when_extraction_unparseable(wiki, monkeypatch):
    # Force the LLM to return junk both times -> pipeline must still write a page.
    monkeypatch.setattr(ingest.llm, "complete", lambda system, user: "not json at all")
    page = ingest.ingest_source("A plain observation with no secrets.", "plain-note")
    assert page.kind == "fact"
    assert store.read_page(page.id).title
    assert page.sources == ["plain-note"]
