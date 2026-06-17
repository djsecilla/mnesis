"""Environment configuration for the mnesis_agents LangGraph layer.

Provider-agnostic by design: the only place that knows about specific LLM
providers is models.py. Everything here is plain env parsing with sane defaults,
and it never imports langchain or mnesis — so it is safe to import in a minimal
environment (and the stub path needs nothing but this + langchain-core).
"""
from __future__ import annotations

import os
from pathlib import Path

#: Supported provider keys for MNESIS_LLM_PROVIDER. Each maps to a LangChain
#: integration in models.py; "openai_compatible" reuses the OpenAI client with a
#: custom base_url (e.g. local vLLM / LM Studio / OpenRouter).
SUPPORTED_PROVIDERS: tuple[str, ...] = (
    "openai",
    "anthropic",
    "google",
    "mistral",
    "bedrock",
    "ollama",
    "openai_compatible",
)


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _opt(name: str) -> str | None:
    """An optional env var: None when unset or blank."""
    v = os.environ.get(name)
    return v if (v is not None and v.strip() != "") else None


# ── LLM provider / model ─────────────────────────────────────────────────────

#: Which LLM provider the model factory builds. One of SUPPORTED_PROVIDERS.
MNESIS_LLM_PROVIDER: str = os.environ.get("MNESIS_LLM_PROVIDER", "openai").strip().lower()

#: Model id/name for the chosen provider (e.g. "gpt-4o-mini", "claude-sonnet-4-6",
#: "llama3.2:3b"). Has no universal default — required for a real (non-stub) model.
MNESIS_LLM_MODEL: str | None = _opt("MNESIS_LLM_MODEL")

#: Base URL passthrough — for ollama / openai_compatible / self-hosted endpoints.
MNESIS_LLM_BASE_URL: str | None = _opt("MNESIS_LLM_BASE_URL")

#: API key passthrough. Usually read by the provider SDK from its own env var
#: (OPENAI_API_KEY, ANTHROPIC_API_KEY, ...); set this to override generically.
MNESIS_LLM_API_KEY: str | None = _opt("MNESIS_LLM_API_KEY")

#: Sampling temperature. Default 0 for deterministic, reproducible agent runs.
MNESIS_LLM_TEMPERATURE: float = float(os.environ.get("MNESIS_LLM_TEMPERATURE", "0"))

# ── Mnesis MCP connection (used in a later prompt) ──────────────────────────

MNESIS_MCP_URL: str = os.environ.get("MNESIS_MCP_URL", "http://localhost:8080/mcp")
MNESIS_MCP_TOKEN: str | None = _opt("MNESIS_MCP_TOKEN")

# ── Agent Skills ─────────────────────────────────────────────────────────────

#: Extra skill directories to scan (os.pathsep-separated), in addition to the
#: always-scanned ``./skills`` (project-level) and the packaged example skills.
MNESIS_AGENTS_SKILLS_DIRS: str = os.environ.get("MNESIS_AGENTS_SKILLS_DIRS", "")

# ── Governance / persistence / audit (F6) ──────────────────────────────────

#: Append-only JSONL run-audit directory (statuses + ids only — never payloads).
MNESIS_AGENTS_AUDIT_DIR: Path = Path(
    os.environ.get("MNESIS_AGENTS_AUDIT_DIR", "./mnesis_agents_runs")
).expanduser()

#: LangGraph checkpointer backend ("sqlite" default; "memory" for ephemeral).
MNESIS_AGENTS_CHECKPOINT_BACKEND: str = os.environ.get(
    "MNESIS_AGENTS_CHECKPOINT_BACKEND", "sqlite"
).strip().lower()

#: SQLite checkpoint DB path (used when backend is "sqlite").
MNESIS_AGENTS_CHECKPOINT_DB: Path = Path(
    os.environ.get("MNESIS_AGENTS_CHECKPOINT_DB", "./mnesis_agents.checkpoints.db")
).expanduser()

#: Default per-run budgets (a profile may override; None/0 = unlimited).
MNESIS_AGENTS_MAX_TOOL_CALLS: int = int(os.environ.get("MNESIS_AGENTS_MAX_TOOL_CALLS", "50"))
MNESIS_AGENTS_MAX_TOKENS: int = int(os.environ.get("MNESIS_AGENTS_MAX_TOKENS", "0"))
MNESIS_AGENTS_WALLCLOCK_SECONDS: float = float(
    os.environ.get("MNESIS_AGENTS_WALLCLOCK_SECONDS", "300")
)


# ── Dream cycle (M4: proposals / reporting / crystallization / schedule) ─────

#: Directory for the dream-cycle proposals queue + persisted reports (defaults to
#: the audit dir, so all agent-run artefacts live together; gitignored).
MNESIS_AGENTS_PROPOSALS_DIR: Path = Path(
    os.environ.get("MNESIS_AGENTS_PROPOSALS_DIR", str(MNESIS_AGENTS_AUDIT_DIR))
).expanduser()

#: Meta-memory: when on, the dream cycle files a concise digest of itself back
#: into Mnesis (so Mnesis records its own dream cycles). Default OFF.
MNESIS_AGENTS_CRYSTALLIZE: bool = _bool("MNESIS_AGENTS_CRYSTALLIZE")

#: Max characters of the crystallized maintenance digest body (bounded write).
MNESIS_AGENTS_CRYSTALLIZE_MAX_CHARS: int = int(
    os.environ.get("MNESIS_AGENTS_CRYSTALLIZE_MAX_CHARS", "1500")
)

#: Dream-cycle cadence. Default a nightly cron (03:00); cron needs the APScheduler
#: extra. Set an interval (seconds) to use the bundled dependency-free scheduler.
MNESIS_AGENTS_DREAM_CRON: str = os.environ.get("MNESIS_AGENTS_DREAM_CRON", "0 3 * * *")
MNESIS_AGENTS_DREAM_INTERVAL_SECONDS: float | None = (
    float(os.environ["MNESIS_AGENTS_DREAM_INTERVAL_SECONDS"])
    if os.environ.get("MNESIS_AGENTS_DREAM_INTERVAL_SECONDS")
    else None
)


def tracing_enabled() -> bool:
    """True only when LangSmith tracing is explicitly turned on via its own env
    (LANGSMITH_TRACING / legacy LANGCHAIN_TRACING_V2). Off by default — a plain
    run sends nothing externally. We never set these ourselves."""
    return _bool("LANGSMITH_TRACING") or _bool("LANGCHAIN_TRACING_V2")


# ── Offline stub ─────────────────────────────────────────────────────────────

#: When set, get_chat_model() returns a deterministic offline fake model — no
#: provider extra, no API key, no network. The whole layer is testable this way.
MNESIS_AGENTS_STUB: bool = _bool("MNESIS_AGENTS_STUB")
