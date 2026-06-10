#!/usr/bin/env python
"""End-to-end demo of the compounding loop, fully offline (stub LLM).

Runs in a throwaway temp wiki + git repo so it never touches the project's own
`wiki/` or history. It walks the whole loop and prints each step:

    ingest A + B  ->  rebuild  ->  query  ->  file_back a digest  ->  query again

One source carries a fake API key, to show redaction happening at the boundary.

Run it with:  uv run python scripts/demo_end_to_end.py
"""

from __future__ import annotations

import os
import subprocess
import tempfile

# Force offline stub mode and a throwaway wiki root BEFORE importing the package,
# since config reads these at import time.
_TMP = tempfile.mkdtemp(prefix="mnesis-demo-")
os.environ["WIKI_LLM_STUB"] = "1"
os.environ["WIKI_ROOT"] = os.path.join(_TMP, "wiki")

from mnesis import config, mcp_server  # noqa: E402  (import after env setup)

# --- Small, self-contained sample sources ---------------------------------

SOURCE_A = (
    "Project Atlas uses Redis as its primary caching layer for hot data. "
    "The auth-migration workstream depends on this cache. Sarah owns the auth migration."
)

# Source B intentionally contains a fake secret to demonstrate redaction.
SOURCE_B = (
    "The Atlas billing service stores invoices in PostgreSQL. "
    "Deploy access uses the API key sk-test1234567890ABCDEFGHijklmnop, "
    "which must be rotated quarterly."
)


def _hr(title: str) -> None:
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def main() -> None:
    config.ensure_dirs()
    subprocess.run(["git", "-C", _TMP, "init", "-q"], check=True)
    subprocess.run(["git", "-C", _TMP, "config", "user.name", "mnesis demo"], check=True)
    subprocess.run(["git", "-C", _TMP, "config", "user.email", "demo@localhost"], check=True)

    print(f"Demo wiki root: {config.WIKI_ROOT}  (offline stub mode)")

    _hr("STEP 1 — Ingest source A (Atlas / Redis)")
    print(mcp_server.wiki_ingest(SOURCE_A, "atlas-architecture"))

    _hr("STEP 2 — Ingest source B (billing / PostgreSQL, contains a fake secret)")
    print(mcp_server.wiki_ingest(SOURCE_B, "billing-notes"))
    saved = (config.SOURCES_DIR / "billing-notes.md").read_text()
    print("\nSaved source on disk (note the secret is gone):")
    print("  " + saved.strip().replace("\n", "\n  "))

    _hr("STEP 3 — Rebuild the search index from Markdown")
    print(mcp_server.wiki_rebuild())

    _hr('STEP 4 — Query "redis caching"')
    print(mcp_server.wiki_query("redis caching"))

    _hr("STEP 5 — Synthesize an answer and file it back as a digest")
    question = "What does Project Atlas use for caching, and what depends on it?"
    answer = (
        "Project Atlas uses Redis as its primary caching layer, and the "
        "auth-migration workstream depends on that cache. Replacing or upgrading "
        "Redis therefore risks the auth migration and should be coordinated with "
        "Sarah, who owns it."
    )
    print(f"Q: {question}")
    print(mcp_server.wiki_file_back(question, answer, quality_score=0.9))

    _hr('STEP 6 — Query "caching" again — the digest now surfaces alongside the facts')
    print(mcp_server.wiki_query("caching"))

    _hr("All pages now in the wiki")
    print(mcp_server.wiki_list())

    print(f"\nDone. (Throwaway demo data left at {_TMP})")


if __name__ == "__main__":
    main()
