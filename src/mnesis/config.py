"""Paths and environment configuration for mnesis.

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

# Repository root: this file is src/mnesis/config.py, so the root is three
# parents up (config.py -> mnesis -> src -> repo root).
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


# --- Phase 2: confidence model constants (all env-overridable) --------------
# The confidence formula lives in confidence.py; these are its tunable inputs.
# Tuning needs no code change — override via the env vars named below.


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw is not None else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw is not None else default


#: Ebbinghaus stability S (days) per decay class — how slowly retention falls.
#: architecture/decisions decay slowly; bugs/transients fast.
STABILITY_DAYS: dict[str, int] = {
    "decision": _env_int("WIKI_STABILITY_DECISION", 365),
    "architecture": _env_int("WIKI_STABILITY_ARCHITECTURE", 365),
    "fact": _env_int("WIKI_STABILITY_FACT", 180),
    "note": _env_int("WIKI_STABILITY_NOTE", 60),
    "transient": _env_int("WIKI_STABILITY_TRANSIENT", 21),
    "bug": _env_int("WIKI_STABILITY_BUG", 21),
}

#: Relative weights of the support and retention terms in the raw blend.
W_SUPPORT: float = _env_float("WIKI_W_SUPPORT", 1.0)
W_RETENTION: float = _env_float("WIKI_W_RETENTION", 1.0)

#: Hard ceiling on a stale page's confidence.
STALE_CAP: float = _env_float("WIKI_STALE_CAP", 0.40)

#: Access boost = min(ACCESS_BOOST_CAP, ACCESS_BOOST_PER * recent_access_count).
ACCESS_BOOST_CAP: float = _env_float("WIKI_ACCESS_BOOST_CAP", 0.10)
ACCESS_BOOST_PER: float = _env_float("WIKI_ACCESS_BOOST_PER", 0.02)

# --- Phase 2: relation-aware ingest -----------------------------------------

#: A contradiction auto-resolves when the winner's confidence exceeds the
#: loser's by at least this margin; otherwise both pages coexist and are queued.
AUTO_RESOLVE_MARGIN: float = _env_float("WIKI_AUTO_RESOLVE_MARGIN", 0.25)

#: How many top search hits to consider as candidate existing pages on ingest.
CANDIDATE_TOP_N: int = _env_int("WIKI_CANDIDATE_TOP_N", 5)

# --- Phase 2: decay / lifecycle ---------------------------------------------

#: An active page goes stale only when confidence drops below this AND it has
#: been inactive (no access, no reinforcement) past its decay class's window.
STALE_THRESHOLD: float = _env_float("WIKI_STALE_THRESHOLD", 0.25)

#: Inactivity window (days) per decay class before a low-confidence page may go
#: stale. Slow classes tolerate longer silence; transients/bugs go quiet fast.
INACTIVITY_DAYS: dict[str, int] = {
    "decision": _env_int("WIKI_INACTIVITY_DECISION", 180),
    "architecture": _env_int("WIKI_INACTIVITY_ARCHITECTURE", 180),
    "fact": _env_int("WIKI_INACTIVITY_FACT", 90),
    "note": _env_int("WIKI_INACTIVITY_NOTE", 30),
    "transient": _env_int("WIKI_INACTIVITY_TRANSIENT", 14),
    "bug": _env_int("WIKI_INACTIVITY_BUG", 14),
}

# --- Phase 3: knowledge graph -----------------------------------------------

#: Which GraphBackend implementation to use. The graph is a rebuildable cache,
#: so this is a low-lock-in choice: "sqlite" (embedded, default) today; a Tier-B
#: backend (e.g. Postgres+AGE, Neo4j) implements the same interface and is
#: selected here with no changes elsewhere. (Env prefix kept WIKI_* for codebase
#: consistency; the playbook refers to it as MNESIS_GRAPH_BACKEND.)
GRAPH_BACKEND: str = os.environ.get("WIKI_GRAPH_BACKEND", "sqlite")


def ensure_dirs() -> None:
    """Create the wiki directory tree on demand. Safe to call repeatedly."""
    for d in (WIKI_ROOT, PAGES_DIR, SOURCES_DIR, INDEX_DIR):
        d.mkdir(parents=True, exist_ok=True)
