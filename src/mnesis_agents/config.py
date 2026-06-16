"""Environment configuration for the mnesis_agents LangGraph layer.

Provider-agnostic by design: the only place that knows about specific LLM
providers is models.py. Everything here is plain env parsing with sane defaults,
and it never imports langchain or mnesis — so it is safe to import in a minimal
environment (and the stub path needs nothing but this + langchain-core).
"""
from __future__ import annotations

import os

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

# ── Offline stub ─────────────────────────────────────────────────────────────

#: When set, get_chat_model() returns a deterministic offline fake model — no
#: provider extra, no API key, no network. The whole layer is testable this way.
MNESIS_AGENTS_STUB: bool = _bool("MNESIS_AGENTS_STUB")
