"""Paths and environment configuration for mnesis.

This module is the single place that resolves where the wiki lives on disk and
reads the handful of environment knobs the PoC exposes. It contains no business
logic — later modules (store, ingest, search, llm, mcp_server, cli) build on the
paths and constants defined here.

Conventions (see CLAUDE.md §3):
  - MNESIS_ROOT defaults to ./wiki, resolved relative to the repository root so the
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
    """Resolve MNESIS_ROOT to an absolute path, relative to REPO_ROOT if relative."""
    if not value:
        return REPO_ROOT / "wiki"
    p = Path(value).expanduser()
    return p if p.is_absolute() else (REPO_ROOT / p)


# --- Paths -----------------------------------------------------------------

MNESIS_ROOT: Path = _resolve_root(os.environ.get("MNESIS_ROOT"))
PAGES_DIR: Path = MNESIS_ROOT / "pages"
SOURCES_DIR: Path = MNESIS_ROOT / "sources"
INDEX_DIR: Path = MNESIS_ROOT / ".index"

# --- Environment-configurable settings (all with fallbacks) ----------------

#: Inference provider: "anthropic" (default) or "local" (an Ollama / OpenAI-
#: compatible endpoint, for local-first inference that never leaves the host).
MNESIS_LLM_PROVIDER: str = os.environ.get("MNESIS_LLM_PROVIDER", "anthropic")

#: Model used by the ingestion/extraction LLM (an Anthropic model id by default;
#: an Ollama model tag like "llama3.2:1b" when provider is "local").
MNESIS_LLM_MODEL: str = os.environ.get("MNESIS_LLM_MODEL", "claude-sonnet-4-6")

#: Base URL of the local model server (used only when provider is "local").
MNESIS_LLM_BASE_URL: str = os.environ.get("MNESIS_LLM_BASE_URL", "http://localhost:11434")

#: Read timeout (seconds) for a single LLM completion. Local models on modest
#: hardware can take a while on longer sources, so this is generous by default
#: and env-overridable. The ingest path turns a timeout into a clean error.
MNESIS_LLM_TIMEOUT: float = float(os.environ.get("MNESIS_LLM_TIMEOUT", "300"))

#: API key passthrough for the broader providers (openai/google/mistral/bedrock/
#: ollama/openai_compatible) reached via the shared multi-LLM factory. Usually the
#: provider SDK reads its own env (OPENAI_API_KEY, …); set this to override.
MNESIS_LLM_API_KEY: str | None = os.environ.get("MNESIS_LLM_API_KEY") or None

#: Sampling temperature for shared-factory providers (the native anthropic/local
#: paths keep their own behaviour). Default 0 for reproducible extraction.
MNESIS_LLM_TEMPERATURE: float = float(os.environ.get("MNESIS_LLM_TEMPERATURE", "0"))

#: Quality gate for filing answers back as digest pages.
MNESIS_FILEBACK_THRESHOLD: float = float(os.environ.get("MNESIS_FILEBACK_THRESHOLD", "0.7"))


def _read_stub_flag() -> bool:
    """True when the LLM client should return deterministic canned output.

    Explicit MNESIS_LLM_STUB wins. Otherwise the stub auto-enables only for the
    Anthropic provider when no API key is present (so tests/demo run offline) —
    the local provider has its own endpoint and never falls back to the stub.
    """
    raw = os.environ.get("MNESIS_LLM_STUB")
    if raw is not None:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if os.environ.get("MNESIS_LLM_PROVIDER", "anthropic") == "local":
        return False
    return not os.environ.get("ANTHROPIC_API_KEY")


#: When True, the LLM client returns deterministic stub output (offline mode).
MNESIS_LLM_STUB: bool = _read_stub_flag()


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
    "decision": _env_int("MNESIS_STABILITY_DECISION", 365),
    "architecture": _env_int("MNESIS_STABILITY_ARCHITECTURE", 365),
    "fact": _env_int("MNESIS_STABILITY_FACT", 180),
    "note": _env_int("MNESIS_STABILITY_NOTE", 60),
    "transient": _env_int("MNESIS_STABILITY_TRANSIENT", 21),
    "bug": _env_int("MNESIS_STABILITY_BUG", 21),
}

#: Relative weights of the support and retention terms in the raw blend.
W_SUPPORT: float = _env_float("MNESIS_W_SUPPORT", 1.0)
W_RETENTION: float = _env_float("MNESIS_W_RETENTION", 1.0)

#: Hard ceiling on a stale page's confidence.
STALE_CAP: float = _env_float("MNESIS_STALE_CAP", 0.40)

#: Access boost = min(ACCESS_BOOST_CAP, ACCESS_BOOST_PER * recent_access_count).
ACCESS_BOOST_CAP: float = _env_float("MNESIS_ACCESS_BOOST_CAP", 0.10)
ACCESS_BOOST_PER: float = _env_float("MNESIS_ACCESS_BOOST_PER", 0.02)

# --- Phase 2: relation-aware ingest -----------------------------------------

#: A contradiction auto-resolves when the winner's confidence exceeds the
#: loser's by at least this margin; otherwise both pages coexist and are queued.
AUTO_RESOLVE_MARGIN: float = _env_float("MNESIS_AUTO_RESOLVE_MARGIN", 0.25)

#: How many top search hits to consider as candidate existing pages on ingest.
CANDIDATE_TOP_N: int = _env_int("MNESIS_CANDIDATE_TOP_N", 5)

# --- Phase 2: decay / lifecycle ---------------------------------------------

#: An active page goes stale only when confidence drops below this AND it has
#: been inactive (no access, no reinforcement) past its decay class's window.
STALE_THRESHOLD: float = _env_float("MNESIS_STALE_THRESHOLD", 0.25)

#: Inactivity window (days) per decay class before a low-confidence page may go
#: stale. Slow classes tolerate longer silence; transients/bugs go quiet fast.
INACTIVITY_DAYS: dict[str, int] = {
    "decision": _env_int("MNESIS_INACTIVITY_DECISION", 180),
    "architecture": _env_int("MNESIS_INACTIVITY_ARCHITECTURE", 180),
    "fact": _env_int("MNESIS_INACTIVITY_FACT", 90),
    "note": _env_int("MNESIS_INACTIVITY_NOTE", 30),
    "transient": _env_int("MNESIS_INACTIVITY_TRANSIENT", 14),
    "bug": _env_int("MNESIS_INACTIVITY_BUG", 14),
}

# --- Phase 3: knowledge graph -----------------------------------------------

#: Which GraphBackend implementation to use. The graph is a rebuildable cache,
#: so this is a low-lock-in choice: "sqlite" (embedded, default) today; a Tier-B
#: backend (e.g. Postgres+AGE, Neo4j) implements the same interface and is
#: selected here with no changes elsewhere. (Env prefix kept MNESIS_* for codebase
#: consistency; the playbook refers to it as MNESIS_GRAPH_BACKEND.)
GRAPH_BACKEND: str = os.environ.get("MNESIS_GRAPH_BACKEND", "sqlite")

#: Graph-augmented query: how far to expand from a resolved entity, and the
#: additive proximity boost (decaying per hop) folded into ranking.
GRAPH_QUERY_DEPTH: int = _env_int("MNESIS_GRAPH_QUERY_DEPTH", 2)
GRAPH_PROXIMITY_BASE: float = _env_float("MNESIS_GRAPH_PROXIMITY_BASE", 0.25)
GRAPH_PROXIMITY_DECAY: float = _env_float("MNESIS_GRAPH_PROXIMITY_DECAY", 0.5)

#: Custom predicate vocabulary for the knowledge graph — a comma-separated list
#: that REPLACES the built-in default set when non-empty (e.g.
#: "uses,part_of,located_in,related_to"). Entries are normalised to snake_case.
#: The structural predicates ``supersedes`` and ``contradicts`` are always
#: included regardless (the graph emits them). Resolved in ``vocab.py``; see
#: CLAUDE.md §6 for the trade-offs of a long list.
MNESIS_PREDICATES: str = os.environ.get("MNESIS_PREDICATES", "")

#: Custom entity-type vocabulary — a comma-separated list of the ``type`` in a
#: ``type:value`` entity ref, REPLACING the built-in default when non-empty
#: (e.g. "person,org,place,event,concept"). Entries are normalised to snake_case;
#: ``page`` is reserved (structural page nodes) and is dropped if supplied. The
#: Web UI assigns distinct colours only to the built-in six types — custom types
#: render in the fallback colour unless you add CSS vars. See CLAUDE.md §6.
MNESIS_ENTITY_TYPES: str = os.environ.get("MNESIS_ENTITY_TYPES", "")

#: Predicates whose direction is not meaningful (A rel B ⟺ B rel A) — a
#: comma-separated list REPLACING the default (`contradicts,related_to`) when set.
#: A symmetric edge is stored once (reciprocal A→B / B→A assertions collapse into
#: a single edge), traversed from either endpoint, and rendered without a
#: direction arrow. Intersected with the active predicate set. See CLAUDE.md §6.
MNESIS_SYMMETRIC_PREDICATES: str = os.environ.get("MNESIS_SYMMETRIC_PREDICATES", "contradicts,related_to")

# --- MCP server transport ---------------------------------------------------

#: Transport for the MCP server: "stdio" (default; local Claude Code spawns it
#: as a subprocess) or "http" (networked, for container deployment).
MNESIS_MCP_TRANSPORT: str = os.environ.get("MNESIS_MCP_TRANSPORT", "stdio")

#: HTTP-mode bind address/port (only used when transport is "http").
MNESIS_MCP_HOST: str = os.environ.get("MNESIS_MCP_HOST", "0.0.0.0")
MNESIS_MCP_PORT: int = _env_int("MNESIS_MCP_PORT", 8080)

#: Optional bearer token for HTTP mode. If set, every tool call must present
#: ``Authorization: Bearer <token>``. Empty = no auth (privileged endpoint).
MNESIS_MCP_TOKEN: str = os.environ.get("MNESIS_MCP_TOKEN", "")

#: Host-header allowlist for the HTTP MCP endpoint's DNS-rebinding protection
#: (comma-separated; each entry an exact ``host:port`` or a ``host:*`` wildcard).
#: Empty keeps FastMCP's secure default (localhost only) — correct for host-side
#: agents reaching ``localhost:8080``. A networked deployment where clients reach
#: the server by another name (e.g. the dockerized agent connecting to
#: ``mnesis:8080``) must list that name here, else the server returns 421.
MNESIS_MCP_ALLOWED_HOSTS: str = os.environ.get("MNESIS_MCP_ALLOWED_HOSTS", "")

#: Max bytes accepted by the ingestion upload endpoints (pasted text or file).
MNESIS_MAX_UPLOAD_BYTES: int = _env_int("MNESIS_MAX_UPLOAD_BYTES", 2_000_000)


def ensure_dirs() -> None:
    """Create the wiki directory tree on demand. Safe to call repeatedly."""
    for d in (MNESIS_ROOT, PAGES_DIR, SOURCES_DIR, INDEX_DIR):
        d.mkdir(parents=True, exist_ok=True)
