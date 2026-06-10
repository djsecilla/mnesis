"""End-to-end test of the compounding loop (offline stub mode).

ingest A + B -> query -> file_back -> query again, asserting the whole loop:
pages created, secret redacted everywhere, search works, the digest is filed
above threshold and then retrievable, git history is right, and a fresh rebuild
reproduces the search results.
"""

from __future__ import annotations

import subprocess

import pytest

from mnesis import config, mcp_server, search, store

FAKE_SECRET = "sk-test1234567890ABCDEFGHijklmnop"

SOURCE_A = (
    "Project Atlas uses Redis as its primary caching layer. "
    "The auth migration depends on this cache; Sarah owns it."
)
SOURCE_B = (
    "The billing service stores invoices in PostgreSQL. "
    f"Deploy key {FAKE_SECRET} must be rotated quarterly."
)


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


def _git_log(repo) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(repo), "log", "--pretty=%s"], capture_output=True, text=True
    )
    return out.stdout.splitlines()


def test_full_compounding_loop(wiki):
    # --- ingest both sources ---
    mcp_server.wiki_ingest(SOURCE_A, "atlas-architecture")
    mcp_server.wiki_ingest(SOURCE_B, "billing-notes")

    facts = store.list_pages(kind="fact")
    assert len(facts) == 2

    # --- secret is redacted EVERYWHERE: page bodies and the saved source ---
    for p in facts:
        assert FAKE_SECRET not in (config.PAGES_DIR / f"{p.id}.md").read_text()
    saved_b = (config.SOURCES_DIR / "billing-notes.md").read_text()
    assert FAKE_SECRET not in saved_b
    assert "[REDACTED:SECRET]" in saved_b  # caught, not merely absent

    # --- search finds the right page ---
    search.rebuild()
    hits = search.search("redis caching")
    assert hits and "redis" in hits[0].title.lower()
    atlas_id = hits[0].id

    # --- file_back above threshold creates a digest ---
    question = "What does Atlas use for caching?"
    answer = "Atlas uses Redis as its primary caching layer; the auth migration depends on it."
    result = mcp_server.wiki_file_back(question, answer, quality_score=0.9)
    assert result.startswith("filed digest:")
    digests = store.list_pages(kind="digest")
    assert len(digests) == 1
    assert "kind:digest" in digests[0].tags
    assert digests[0].question == question

    # --- follow-up query retrieves the digest ---
    digest_hits = {h.id for h in search.search("caching")}
    assert digests[0].id in digest_hits
    assert atlas_id in digest_hits  # original fact still surfaces too

    # --- git history contains the expected commits ---
    msgs = _git_log(wiki)
    assert "mnesis: source atlas-architecture" in msgs
    assert "mnesis: source billing-notes" in msgs
    assert sum(m.startswith("mnesis: write ") for m in msgs) == 3  # 2 facts + 1 digest

    # --- a fresh rebuild reproduces the search results ---
    before = [(h.id, h.bm25_score, h.snippet) for h in search.search("caching")]
    (config.INDEX_DIR / "wiki.db").unlink()
    search.rebuild()
    after = [(h.id, h.bm25_score, h.snippet) for h in search.search("caching")]
    assert before == after
