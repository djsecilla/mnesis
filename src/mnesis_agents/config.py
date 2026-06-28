"""Environment configuration for the mnesis_agents LangGraph layer.

Provider-agnostic by design: the only place that knows about specific LLM
providers is models.py. Everything here is plain env parsing with sane defaults,
and it never imports langchain or mnesis — so it is safe to import in a minimal
environment (and the stub path needs nothing but this + langchain-core).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path


def now_iso() -> str:
    """Current UTC time as an ISO 8601 isoformat string (with UTC offset).

    Defined here — the leaf of the import graph — so every agents module that
    needs a UTC timestamp can import it without creating cycles.  The format
    (``datetime.isoformat()``) matches the convention already used throughout
    the agents layer.
    """
    return datetime.now(timezone.utc).isoformat()

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

#: Whether the deployed runtime (`mnesis-agents run`) registers the notes-inbox
#: connector + writing agent (watch the inbox and ingest). Default ON.
MNESIS_NOTES_ENABLED: bool = _bool("MNESIS_NOTES_ENABLED", True)

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

# ── Outbound channels (A1: action agent delivery) ───────────────────────────

#: Where the inert DraftOutboxChannel writes draft artifacts (a brief, a message
#: draft). NOTHING is sent anywhere — drafts wait here for a human. Gitignored.
MNESIS_ACTION_OUTBOX: Path = Path(
    os.environ.get("MNESIS_ACTION_OUTBOX", "./action_outbox")
).expanduser()

#: Where the inert LocalNotifyChannel appends operator-only notifications (JSONL).
#: Defaults under the outbox. No third-party recipient is ever involved.
MNESIS_ACTION_NOTIFY_FILE: Path = Path(
    os.environ.get("MNESIS_ACTION_NOTIFY_FILE", str(MNESIS_ACTION_OUTBOX / "notifications.jsonl"))
).expanduser()

#: The approval gate (A2): EVERY action is gated (proposed → human-approved) before
#: any channel runs. This flag is the *future* escape hatch that could let an INERT
#: channel auto-run — **leave it OFF**. EXTERNAL channels are ALWAYS gated
#: regardless of this flag (the always-gated rule, enforced in the gate).
MNESIS_ACTIONS_AUTO_RUN_INERT: bool = _bool("MNESIS_ACTIONS_AUTO_RUN_INERT", False)

# ── Action agent (A4) ───────────────────────────────────────────────────────

#: action_type -> compose-skill mapping (comma-separated ``type:skill`` pairs).
#: Adding an action is: a compose skill + ONE entry here. Default maps the
#: prepare-meeting-brief action to its skill.
MNESIS_AGENTS_ACTION_SKILLS: str = os.environ.get(
    "MNESIS_AGENTS_ACTION_SKILLS", "prepare-meeting-brief:prepare-meeting-brief"
)


def action_skill_map() -> dict[str, str]:
    """Resolve ``MNESIS_AGENTS_ACTION_SKILLS`` to a ``{action_type: skill}`` dict."""
    out: dict[str, str] = {}
    for pair in MNESIS_AGENTS_ACTION_SKILLS.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        atype, skill = pair.split(":", 1)
        if atype.strip() and skill.strip():
            out[atype.strip()] = skill.strip()
    return out


#: The default delivery channel the action agent proposes to (POLICY, never from
#: content). The inert draft outbox — nothing is sent; a human approves at the gate.
MNESIS_AGENTS_ACTION_CHANNEL: str = os.environ.get("MNESIS_AGENTS_ACTION_CHANNEL", "draft-outbox")

#: Whether the runtime registers the action agent's F5 **schedule** hook (compose
#: proposal-only briefs for *provided* contexts on a cadence). Default OFF — the
#: action agent is primarily on-demand (`mnesis-agents action …`); there is no real
#: meeting-context source yet (a future inbound connector). The on-demand CLI and
#: the approvals CLI are available regardless of this flag.
MNESIS_AGENTS_ACTIONS_SCHEDULE_ENABLED: bool = _bool("MNESIS_AGENTS_ACTIONS_SCHEDULE_ENABLED", False)

#: Optional JSON file (a list of meeting-context dicts) the scheduled action hook
#: composes briefs for. Absent/empty → the hook is idle (composes nothing).
MNESIS_ACTIONS_CONTEXTS_FILE: str | None = _opt("MNESIS_ACTIONS_CONTEXTS_FILE")

#: Cadence (seconds) for the scheduled action hook (when enabled).
MNESIS_ACTIONS_SCHEDULE_INTERVAL_SECONDS: float = float(
    os.environ.get("MNESIS_ACTIONS_SCHEDULE_INTERVAL_SECONDS", "3600")
)

# ── Egress control plane (E1) — DEFAULT-DENY ─────────────────────────────────
# The reusable gate EVERY future risk_class=external channel must pass through.
# With no configuration, nothing may egress.

#: Master switch. When false (the default), NO external send is permitted at all.
MNESIS_EGRESS_ENABLED: bool = _bool("MNESIS_EGRESS_ENABLED", False)

#: Global kill-switch. When set, ALL egress is denied immediately — overrides
#: everything (including ``MNESIS_EGRESS_ENABLED``).
MNESIS_EGRESS_KILL: bool = _bool("MNESIS_EGRESS_KILL", False)

#: Recipient allowlist — comma-separated exact addresses (``ops@example.com``)
#: and/or domains (``example.com`` or ``@example.com``). **Default empty → no
#: recipient is allowed** (add the operator's address to make it operator-only).
MNESIS_EGRESS_RECIPIENT_ALLOWLIST: str = os.environ.get("MNESIS_EGRESS_RECIPIENT_ALLOWLIST", "")

#: Endpoint allowlist — comma-separated permitted send targets (``smtp.example.com``
#: or ``smtp.example.com:587``). Default empty → no endpoint is allowed.
MNESIS_EGRESS_ENDPOINT_ALLOWLIST: str = os.environ.get("MNESIS_EGRESS_ENDPOINT_ALLOWLIST", "")

#: Rate limits (sends per window) and daily quotas (sends per UTC day), per
#: recipient and global. ``0`` = deny all; negative = unlimited.
MNESIS_EGRESS_RATE_LIMIT: int = int(os.environ.get("MNESIS_EGRESS_RATE_LIMIT", "10"))
MNESIS_EGRESS_RATE_WINDOW_SECONDS: float = float(
    os.environ.get("MNESIS_EGRESS_RATE_WINDOW_SECONDS", "3600")
)
MNESIS_EGRESS_DAILY_QUOTA: int = int(os.environ.get("MNESIS_EGRESS_DAILY_QUOTA", "50"))
MNESIS_EGRESS_GLOBAL_RATE_LIMIT: int = int(os.environ.get("MNESIS_EGRESS_GLOBAL_RATE_LIMIT", "30"))
MNESIS_EGRESS_GLOBAL_DAILY_QUOTA: int = int(os.environ.get("MNESIS_EGRESS_GLOBAL_DAILY_QUOTA", "200"))

#: Where the egress quota/rate ledger persists (gitignored).
MNESIS_EGRESS_STATE_DIR: Path = Path(
    os.environ.get("MNESIS_EGRESS_STATE_DIR", str(MNESIS_AGENTS_CONNECTOR_STATE_DIR))
).expanduser()

# ── Email send channel (E2/E5) — DISABLED + DRY-RUN by default ───────────────
# The first external (risk_class=external) channel. It is not even a *registered*
# delivery option unless explicitly enabled, and even then sends NOTHING unless
# dry-run is disabled AND the egress control plane (above) permits it.

#: Register the email channel as an available delivery option (E5). Default OFF —
#: when unset, `email` is not a known channel at all, so an email proposal fails
#: closed (unknown channel). Turning it on still leaves it dry-run + egress-gated.
MNESIS_EMAIL_ENABLED: bool = _bool("MNESIS_EMAIL_ENABLED", False)

#: Dry-run (default true): render the exact message but send NOTHING. A live send
#: requires this to be false AND egress enabled + allowlisted + within quota.
MNESIS_EMAIL_DRYRUN: bool = _bool("MNESIS_EMAIL_DRYRUN", True)

#: The configured sender (From). Required for a live send.
MNESIS_EMAIL_FROM: str | None = _opt("MNESIS_EMAIL_FROM")

#: SMTP endpoint (must be on the egress endpoint allowlist) + TLS + credentials.
#: Credentials come from env / a secret store — NEVER in code or the image.
MNESIS_SMTP_HOST: str | None = _opt("MNESIS_SMTP_HOST")
MNESIS_SMTP_PORT: int = int(os.environ.get("MNESIS_SMTP_PORT", "587"))
MNESIS_SMTP_USERNAME: str | None = _opt("MNESIS_SMTP_USERNAME")
MNESIS_SMTP_PASSWORD: str | None = _opt("MNESIS_SMTP_PASSWORD")
#: TLS is REQUIRED for a live send (STARTTLS). Leaving this on is the only sane
#: setting; a live send with it off is refused (blocked).
MNESIS_EMAIL_STARTTLS: bool = _bool("MNESIS_EMAIL_STARTTLS", True)
MNESIS_EMAIL_TIMEOUT: float = float(os.environ.get("MNESIS_EMAIL_TIMEOUT", "30"))

#: The immutable, append-only, hash-chained **send-audit** log — one record per
#: external send attempt (ids, recipient, endpoint, content hash, decision,
#: status; NEVER the body or a secret). Gitignored.
MNESIS_SEND_AUDIT_FILE: Path = Path(
    os.environ.get("MNESIS_SEND_AUDIT_FILE", str(MNESIS_EGRESS_STATE_DIR / "send_audit.jsonl"))
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
