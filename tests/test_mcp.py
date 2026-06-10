"""Tests for the MCP server tools, called directly (not over the wire) in stub mode."""

from __future__ import annotations

import subprocess

import pytest

from mnesis import config, mcp_server, store

FAKE_SECRET = "sk-test1234567890ABCDEFGHijklmnop"


@pytest.fixture()
def wiki(tmp_path, monkeypatch):
    root = tmp_path / "wiki"
    (root / "pages").mkdir(parents=True)
    (root / "sources").mkdir(parents=True)
    monkeypatch.setattr(config, "WIKI_ROOT", root)
    monkeypatch.setattr(config, "PAGES_DIR", root / "pages")
    monkeypatch.setattr(config, "SOURCES_DIR", root / "sources")
    monkeypatch.setattr(config, "INDEX_DIR", root / ".index")
    monkeypatch.setattr(config, "WIKI_LLM_STUB", True)
    monkeypatch.setattr(config, "WIKI_FILEBACK_THRESHOLD", 0.7)

    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@localhost"], check=True
    )
    return tmp_path


def test_wiki_ingest_reports_summary_and_redacts(wiki):
    out = mcp_server.wiki_ingest(
        f"Project Atlas uses Redis for caching. Deploy key {FAKE_SECRET}.",
        "atlas-notes",
    )
    assert "ingested page:" in out
    assert "redactions: 1" in out  # the fake secret was caught

    # The secret never reaches the page on disk.
    pages = store.list_pages()
    assert len(pages) == 1
    page_text = (config.PAGES_DIR / f"{pages[0].id}.md").read_text()
    assert FAKE_SECRET not in page_text


def test_query_surfaces_ingested_page(wiki):
    mcp_server.wiki_ingest("Project Atlas uses Redis as its caching layer.", "atlas")
    out = mcp_server.wiki_query("redis caching")
    assert "no results" not in out
    assert "redis" in out.lower()


def test_file_back_above_threshold_creates_digest(wiki):
    mcp_server.wiki_ingest("Project Atlas uses Redis for caching.", "atlas")

    result = mcp_server.wiki_file_back(
        "What does Atlas use for caching?",
        "Atlas uses Redis as its primary caching layer, per the architecture notes.",
        quality_score=0.9,
    )
    assert result.startswith("filed digest:")

    digests = store.list_pages(kind="digest")
    assert len(digests) == 1
    d = digests[0]
    assert d.kind == "digest"
    assert "kind:digest" in d.tags
    assert d.question == "What does Atlas use for caching?"

    # The filed answer is now retrievable (compounding).
    assert "no results" not in mcp_server.wiki_query("caching")


def test_file_back_below_threshold_files_nothing(wiki):
    before = len(store.list_pages())
    result = mcp_server.wiki_file_back("Q?", "Too thin.", quality_score=0.3)
    assert "below threshold" in result
    assert len(store.list_pages()) == before  # nothing written


def test_file_back_heuristic_when_no_score(wiki):
    # A short answer scores below threshold under the heuristic.
    assert "below threshold" in mcp_server.wiki_file_back("Q?", "Three words only")
    # A long, developed answer clears it.
    long_answer = " ".join(["word"] * 30)
    assert mcp_server.wiki_file_back("Q long?", long_answer).startswith("filed digest:")


def test_get_and_list_and_rebuild(wiki):
    mcp_server.wiki_ingest("Sarah owns the auth migration.", "sarah-note")
    pid = store.list_pages()[0].id

    md = mcp_server.wiki_get(pid)
    assert md.startswith("---")  # full frontmatter Markdown
    assert "no such page" not in mcp_server.wiki_get(pid)
    assert "no such page" in mcp_server.wiki_get("does-not-exist")

    assert pid in mcp_server.wiki_list()
    assert "rebuilt index from 1 page" in mcp_server.wiki_rebuild()
