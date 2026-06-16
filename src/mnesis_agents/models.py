"""Provider-agnostic chat-model factory for the agents layer.

Thin wrapper over the shared :mod:`mnesis_llm.factory`: it resolves provider/model
from ``mnesis_agents.config`` (overridable for tests), applies the env-specific
validation messages, and delegates the actual model construction. The offline
stub short-circuits with no provider extra and no network.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mnesis_llm.factory import (  # shared with the mnesis core
    SUPPORTED_PROVIDERS,
    ModelProviderNotInstalled,
    get_chat_model as _factory_get_chat_model,
    make_stub_model,
)

from . import config

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel

__all__ = ["get_chat_model", "make_stub_model", "ModelProviderNotInstalled", "SUPPORTED_PROVIDERS"]


def get_chat_model(**overrides: Any) -> "BaseChatModel":
    """Build the configured chat model (or the offline stub).

    ``MNESIS_AGENTS_STUB`` short-circuits to the offline fake. Otherwise the
    provider/model/temperature/base_url/api_key come from config (overrides win).
    """
    if config.MNESIS_AGENTS_STUB:
        return make_stub_model(overrides.get("stub_responses"))

    provider = str(overrides.get("provider", config.MNESIS_LLM_PROVIDER)).strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(
            f"Unsupported MNESIS_LLM_PROVIDER {provider!r}; "
            f"must be one of {', '.join(SUPPORTED_PROVIDERS)} "
            f"(or set MNESIS_AGENTS_STUB=1 for the offline stub)."
        )
    model = overrides.get("model", config.MNESIS_LLM_MODEL)
    if not model:
        raise ValueError(
            "MNESIS_LLM_MODEL is required to build a real model "
            "(or set MNESIS_AGENTS_STUB=1 for the offline stub)."
        )

    return _factory_get_chat_model(
        provider, model,
        base_url=overrides.get("base_url", config.MNESIS_LLM_BASE_URL),
        api_key=overrides.get("api_key", config.MNESIS_LLM_API_KEY),
        temperature=overrides.get("temperature", config.MNESIS_LLM_TEMPERATURE),
    )
