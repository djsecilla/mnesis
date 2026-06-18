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

# ── Source connectors (W1) ──────────────────────────────────────────────────

#: Durable state for source connectors (the processed-item ledger that makes
#: detection idempotent). A subdir of the agents' artefact dir (gitignored).
MNESIS_AGENTS_CONNECTOR_STATE_DIR: Path = Path(
    os.environ.get("MNESIS_AGENTS_CONNECTOR_STATE_DIR", str(MNESIS_AGENTS_AUDIT_DIR / "connectors"))
).expanduser()

#: Notes-inbox connector — the folder it watches for new/changed .md/.txt notes.
MNESIS_NOTES_INBOX: Path = Path(
    os.environ.get("MNESIS_NOTES_INBOX", "./notes_inbox")
).expanduser()

#: Detection mode: "poll" (timed rescans; no extra dep) or "watch" (filesystem
#: events via watchdog, falling back to poll if watchdog isn't installed).
MNESIS_NOTES_MODE: str = os.environ.get("MNESIS_NOTES_MODE", "poll").strip().lower()

#: Seconds between rescans in poll mode (also the debounce floor in watch mode).
MNESIS_NOTES_POLL_INTERVAL: float = float(os.environ.get("MNESIS_NOTES_POLL_INTERVAL", "2"))

#: Max bytes a single note may be; larger files surface as an error, not a crash.
MNESIS_NOTES_MAX_BYTES: int = int(os.environ.get("MNESIS_NOTES_MAX_BYTES", "1000000"))

#: Comma-separated file extensions the notes inbox ingests (lowercased).
MNESIS_NOTES_SUFFIXES: str = os.environ.get("MNESIS_NOTES_SUFFIXES", ".md,.txt")

# ── Writing agent (W3) ──────────────────────────────────────────────────────

#: source_type -> parse-skill mapping (comma-separated ``type:skill`` pairs).
#: Adding a source is: connector + parse skill + ONE entry here. Default maps the
#: notes connector to the parse-note skill.
MNESIS_AGENTS_PARSE_SKILLS: str = os.environ.get("MNESIS_AGENTS_PARSE_SKILLS", "notes:parse-note")


def parse_skill_map() -> dict[str, str]:
    """Resolve ``MNESIS_AGENTS_PARSE_SKILLS`` to a ``{source_type: skill}`` dict."""
    out: dict[str, str] = {}
    for pair in MNESIS_AGENTS_PARSE_SKILLS.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        stype, skill = pair.split(":", 1)
        if stype.strip() and skill.strip():
            out[stype.strip()] = skill.strip()
    return out


#: Source types whose ingest requires human approval (an F6-style gate) before it
#: happens. Comma-separated. Empty by default — the trusted notes inbox
#: auto-ingests; configure untrusted sources here to hold them for review.
MNESIS_AGENTS_APPROVAL_SOURCE_TYPES: str = os.environ.get("MNESIS_AGENTS_APPROVAL_SOURCE_TYPES", "")


def approval_source_types() -> frozenset[str]:
    return frozenset(
        s.strip() for s in MNESIS_AGENTS_APPROVAL_SOURCE_TYPES.split(",") if s.strip()
    )

# ── Writing pipeline robustness (W4) ────────────────────────────────────────

#: Transient-failure retry policy (exponential backoff) for an ingest.
MNESIS_AGENTS_WRITE_MAX_RETRIES: int = int(os.environ.get("MNESIS_AGENTS_WRITE_MAX_RETRIES", "3"))
MNESIS_AGENTS_WRITE_BACKOFF_BASE: float = float(
    os.environ.get("MNESIS_AGENTS_WRITE_BACKOFF_BASE", "0.5")
)
MNESIS_AGENTS_WRITE_BACKOFF_FACTOR: float = float(
    os.environ.get("MNESIS_AGENTS_WRITE_BACKOFF_FACTOR", "2")
)

#: Max notes processed concurrently in a batch/burst (bounded concurrency).
MNESIS_AGENTS_WRITE_CONCURRENCY: int = int(os.environ.get("MNESIS_AGENTS_WRITE_CONCURRENCY", "4"))

#: Dead-letter store for poison items (repeatedly-failing parse/ingest). JSONL
#: under the connector state dir by default (gitignored).
MNESIS_AGENTS_DEAD_LETTER_DIR: Path = Path(
    os.environ.get("MNESIS_AGENTS_DEAD_LETTER_DIR", str(MNESIS_AGENTS_CONNECTOR_STATE_DIR))
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

#: Whether the deployed runtime (`mnesis-agents run`) registers the scheduled
#: dream-cycle maintenance agent. Default ON — it is the single owner of periodic
#: maintenance (the D5 sidecar is retired). Set 0 to run an idle runtime.
MNESIS_AGENTS_DREAM_ENABLED: bool = _bool("MNESIS_AGENTS_DREAM_ENABLED", True)

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
