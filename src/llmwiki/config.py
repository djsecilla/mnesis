"""Paths and environment configuration for LLM Wiki v2.

This module is the single place that resolves where the wiki lives on disk and
reads the handful of environment knobs the PoC exposes. It contains no business
logic — later modules (store, ingest, search, llm, mcp_server, cli) build on the
paths and constants defined here.

Conventions (see CLAUDE.md §3):
  - WIKI_ROOT defaults to ./wiki, resolved relative to the repository root so the
    package behaves the same regardless of the current working directory.
  - Pages and redacted sources are tracked; the SQLite index under .index/ is a
    rebuildable cache and is gitignored.
"""

from __future__ import annotations

import os
from pathlib import Path

# Repository root: this file is src/llmwiki/config.py, so the root is three
# parents up (config.py -> llmwiki -> src -> repo root).
REPO_ROOT: Path = Path(__file__).resolve().parents[2]


def _resolve_root(value: str | None) -> Path:
    """Resolve WIKI_ROOT to an absolute path, relative to REPO_ROOT if relative."""
    if not value:
        return REPO_ROOT / "wiki"
    p = Path(value).expanduser()
    return p if p.is_absolute() else (REPO_ROOT / p)


# --- Paths -----------------------------------------------------------------

WIKI_ROOT: Path = _resolve_root(os.environ.get("WIKI_ROOT"))
PAGES_DIR: Path = WIKI_ROOT / "pages"
SOURCES_DIR: Path = WIKI_ROOT / "sources"
INDEX_DIR: Path = WIKI_ROOT / ".index"

# --- Environment-configurable settings (all with fallbacks) ----------------

#: Model used by the ingestion/extraction LLM.
WIKI_LLM_MODEL: str = os.environ.get("WIKI_LLM_MODEL", "claude-sonnet-4-6")

#: Quality gate for filing answers back as digest pages.
WIKI_FILEBACK_THRESHOLD: float = float(os.environ.get("WIKI_FILEBACK_THRESHOLD", "0.7"))


def _read_stub_flag() -> bool:
    """True when the LLM client should return deterministic canned output.

    Enabled when WIKI_LLM_STUB is set to a truthy value, or when no Anthropic API
    key is present (so tests and the demo run offline by default).
    """
    raw = os.environ.get("WIKI_LLM_STUB")
    if raw is not None:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return not os.environ.get("ANTHROPIC_API_KEY")


#: When True, the LLM client returns deterministic stub output (offline mode).
WIKI_LLM_STUB: bool = _read_stub_flag()


def ensure_dirs() -> None:
    """Create the wiki directory tree on demand. Safe to call repeatedly."""
    for d in (WIKI_ROOT, PAGES_DIR, SOURCES_DIR, INDEX_DIR):
        d.mkdir(parents=True, exist_ok=True)
