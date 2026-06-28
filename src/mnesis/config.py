"""Paths and environment configuration for mnesis.

This module is the single place that resolves where the wiki lives on disk and
reads the handful of environment knobs the PoC exposes. It contains no business
logic — later modules (store, ingest, search, llm, mcp_server, cli) build on the
paths and constants defined here.

Conventions (see CLAUDE.md §3):
  - MNESIS_ROOT defaults to ./wiki, resolved relative to the repository root so the
    package behaves the same regardless of the current working directory. It is the
    **multitenant data root** (`DATA_ROOT`): tenants live under ``tenants/<id>/``
    and the tenant registry is ``registry.json`` beside them.
  - There is deliberately **no module-level store path** here (no PAGES_DIR /
    SOURCES_DIR / INDEX_DIR). The canonical store is tenant-scoped: every path is
    resolved from a :class:`mnesis.tenancy.TenantContext` against its own root, so a
    store cannot be reached without first resolving a tenant (CLAUDE.md §3, §16).
  - Within a tenant, pages and redacted sources are tracked; the SQLite caches under
    ``.cache/`` are rebuildable and gitignored.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path


def now_iso() -> str:
    """Current UTC time as an ISO 8601 string (microsecond precision, Z suffix).

    Defined here — the leaf of the import graph — so every module that needs a
    timestamp (tenancy, auth, admin, store, …) can import it without creating
    cycles.  ``store.now_iso`` re-exports this for callers that already import
    from ``store``.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# Repository root: this file is src/mnesis/config.py, so the root is three
# parents up (config.py -> mnesis -> src -> repo root).
REPO_ROOT: Path = Path(__file__).resolve().parents[2]


def _resolve_root(value: str | None) -> Path:
    """Resolve MNESIS_ROOT to an absolute path, relative to REPO_ROOT if relative."""
    if not value:
        return REPO_ROOT / "wiki"
    p = Path(value).expanduser()
    return p if p.is_absolute() else (REPO_ROOT / p)


# --- Multitenant data root -------------------------------------------------
# DATA_ROOT holds every tenant's own root plus the small tenant registry. It is
# NOT itself a store — there are no global pages/sources/index paths here; those
# are resolved per-tenant from a TenantContext (see tenancy.py). MNESIS_ROOT is
# kept as the env name for backward compatibility (it now names the data root).

DATA_ROOT: Path = _resolve_root(os.environ.get("MNESIS_ROOT"))
MNESIS_ROOT: Path = DATA_ROOT  # backward-compatible alias (the data root)

#: Where each tenant's canonical store + caches live: ``tenants/<tenant_id>/``.
TENANTS_DIRNAME: str = "tenants"
#: The tenant registry (metadata) — a small JSON file OUTSIDE any tenant root.
REGISTRY_FILENAME: str = "registry.json"
#: The default tenant a single-tenant deployment runs as (CLAUDE.md §16).
DEFAULT_TENANT_ID: str = os.environ.get("MNESIS_DEFAULT_TENANT", "default")


def tenants_dir() -> Path:
    return DATA_ROOT / TENANTS_DIRNAME


def registry_path() -> Path:
    return DATA_ROOT / REGISTRY_FILENAME


#: The credential store (auth.py) — credentials -> {tenant_id, principal_id, role}.
#: Lives beside the registry, OUTSIDE any tenant root, and holds only HASHED tokens.
CREDENTIALS_FILENAME: str = "credentials.json"


def credentials_path() -> Path:
    return DATA_ROOT / CREDENTIALS_FILENAME

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
#: LEGACY single-tenant path: used only when ``MNESIS_AUTH_ENABLED`` is off.
MNESIS_MCP_TOKEN: str = os.environ.get("MNESIS_MCP_TOKEN", "")

#: When set, the HTTP boundary resolves a per-tenant, per-principal **credential**
#: (auth.py) from the bearer token instead of the legacy single token: the tenant
#: is taken ONLY from the validated credential and an unresolved credential is
#: denied (fail closed, no default-tenant fallback). Off by default so existing
#: single-tenant deployments keep working until credentials are provisioned (T7).
MNESIS_AUTH_ENABLED: bool = os.environ.get("MNESIS_AUTH_ENABLED", "").strip().lower() in {
    "1", "true", "yes", "on",
}

#: Optional server-side pepper mixed into the token hash at rest (defense in depth).
#: Read by auth.py; never logged. Empty is acceptable (tokens are high-entropy).
MNESIS_AUTH_PEPPER: str = os.environ.get("MNESIS_AUTH_PEPPER", "")

#: Global fallback for a new page's visibility (T4) when a tenant has not set its
#: own default. ``shared`` (visible to all principals in the tenant) or ``private``
#: (owner-only). Per-tenant override lives on the Tenant record (registry).
MNESIS_DEFAULT_VISIBILITY: str = os.environ.get("MNESIS_DEFAULT_VISIBILITY", "shared").strip().lower()

# --- Multitenant lifecycle, admin & quotas (T7) ----------------------------

#: Per-tenant resource quotas (fairness + blast-radius). ``0`` = unlimited. A
#: per-tenant override lives on the Tenant record (registry); these are the
#: defaults. Enforced fail-closed at the ingest write boundary (quotas.py).
MNESIS_TENANT_MAX_PAGES: int = _env_int("MNESIS_TENANT_MAX_PAGES", 0)
MNESIS_TENANT_MAX_BYTES: int = _env_int("MNESIS_TENANT_MAX_BYTES", 0)

#: The credential the **admin CLI** resolves to a SYSTEM-ADMIN principal for tenant
#: lifecycle ops (provision/list/suspend/delete). Distinct from any tenant
#: credential; tenant principals can never manage tenants. (admin.py / auth.py)
MNESIS_ADMIN_CREDENTIAL: str | None = os.environ.get("MNESIS_ADMIN_CREDENTIAL") or None

#: The system (NOT tenant) audit log for lifecycle ops — append-only JSONL beside
#: the registry, OUTSIDE any tenant root. Gitignored.
SYSTEM_AUDIT_FILENAME: str = "system_audit.jsonl"


def system_audit_path() -> Path:
    return DATA_ROOT / SYSTEM_AUDIT_FILENAME

#: Host-header allowlist for the HTTP MCP endpoint's DNS-rebinding protection
#: (comma-separated; each entry an exact ``host:port`` or a ``host:*`` wildcard).
#: Empty keeps FastMCP's secure default (localhost only) — correct for host-side
#: agents reaching ``localhost:8080``. A networked deployment where clients reach
#: the server by another name (e.g. the dockerized agent connecting to
#: ``mnesis:8080``) must list that name here, else the server returns 421.
MNESIS_MCP_ALLOWED_HOSTS: str = os.environ.get("MNESIS_MCP_ALLOWED_HOSTS", "")

#: Max bytes accepted by the ingestion upload endpoints (pasted text or file).
MNESIS_MAX_UPLOAD_BYTES: int = _env_int("MNESIS_MAX_UPLOAD_BYTES", 2_000_000)
