"""Provider-agnostic chat-model factory.

This is the ONLY module that knows about specific LLM providers. Everything else
in mnesis_agents depends on ``BaseChatModel``. ``get_chat_model()`` builds the
configured model via LangChain's ``init_chat_model`` (LangChain 1.x), or returns
a deterministic offline fake when ``MNESIS_AGENTS_STUB`` is set.

Imports are lazy on purpose: the module imports with only ``config`` present, so
a minimal install can import it and run the stub path without any provider extra.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import config

if TYPE_CHECKING:  # type-only — no runtime import
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import AIMessage


# MNESIS_LLM_PROVIDER -> (init_chat_model model_provider, optional-extra name).
# "openai_compatible" reuses the OpenAI client pointed at a custom base_url.
_PROVIDER_MAP: dict[str, tuple[str, str]] = {
    "openai": ("openai", "agents-openai"),
    "openai_compatible": ("openai", "agents-openai"),
    "anthropic": ("anthropic", "agents-anthropic"),
    "google": ("google_genai", "agents-google"),
    "mistral": ("mistralai", "agents-mistral"),
    "bedrock": ("bedrock_converse", "agents-bedrock"),
    "ollama": ("ollama", "agents-ollama"),
}


class ModelProviderNotInstalled(RuntimeError):
    """The selected provider's optional dependency isn't installed."""

    def __init__(self, provider: str, extra: str, cause: Exception | None = None) -> None:
        self.provider = provider
        self.extra = extra
        super().__init__(
            f"LLM provider {provider!r} requires an extra that isn't installed. "
            f"Install it with:  pip install -e \".[{extra}]\"  "
            f"(or `uv pip install -e \".[{extra}]\"`)."
            + (f"\nUnderlying import error: {cause}" if cause else "")
        )


def make_stub_model(responses: list[Any] | None = None) -> "BaseChatModel":
    """A deterministic, offline fake chat model — no keys, no network.

    Cycles through ``responses`` (strings or pre-built ``AIMessage``s, the latter
    for scripting tool-calls) forever, so repeated invocations never exhaust.
    Used whenever ``MNESIS_AGENTS_STUB`` is set, and directly in tests.
    """
    from itertools import cycle

    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    class _StubChatModel(GenericFakeChatModel):
        """Fake model that accepts bind_tools (the base raises NotImplementedError).

        The stub ignores bound tools — behaviour is driven entirely by the scripted
        ``messages`` — so binding is a no-op that returns the model itself, which is
        what the agent loop (create_agent) needs to run offline.
        """

        def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ARG002
            return self

    items = responses or ["This is a deterministic mnesis_agents stub response."]
    messages: list[AIMessage] = [
        m if isinstance(m, AIMessage) else AIMessage(content=str(m)) for m in items
    ]
    return _StubChatModel(messages=cycle(messages))


def get_chat_model(**overrides: Any) -> "BaseChatModel":
    """Build the configured chat model (or the offline stub).

    ``MNESIS_AGENTS_STUB`` short-circuits to :func:`make_stub_model` — usable with
    no provider extra and no API key. Otherwise the provider/model/temperature/
    base_url/api_key come from ``config`` (``overrides`` win, for tests/embedding).
    Raises a clear error for an unknown provider, a missing model, or a provider
    whose extra isn't installed.
    """
    if config.MNESIS_AGENTS_STUB:
        return make_stub_model(overrides.get("stub_responses"))

    provider = str(overrides.get("provider", config.MNESIS_LLM_PROVIDER)).strip().lower()
    if provider not in _PROVIDER_MAP:
        raise ValueError(
            f"Unsupported MNESIS_LLM_PROVIDER {provider!r}; "
            f"must be one of {', '.join(config.SUPPORTED_PROVIDERS)} "
            f"(or set MNESIS_AGENTS_STUB=1 for the offline stub)."
        )

    model = overrides.get("model", config.MNESIS_LLM_MODEL)
    if not model:
        raise ValueError(
            "MNESIS_LLM_MODEL is required to build a real model "
            "(or set MNESIS_AGENTS_STUB=1 for the offline stub)."
        )

    lc_provider, extra = _PROVIDER_MAP[provider]

    # Only forward optional knobs that are set, so providers that don't accept a
    # given kwarg aren't handed a stray None.
    kwargs: dict[str, Any] = {
        "model_provider": lc_provider,
        "temperature": overrides.get("temperature", config.MNESIS_LLM_TEMPERATURE),
    }
    base_url = overrides.get("base_url", config.MNESIS_LLM_BASE_URL)
    if base_url:
        kwargs["base_url"] = base_url
    api_key = overrides.get("api_key", config.MNESIS_LLM_API_KEY)
    if api_key:
        kwargs["api_key"] = api_key

    try:
        from langchain.chat_models import init_chat_model
    except ImportError as exc:  # langchain itself (agents core) missing
        raise ModelProviderNotInstalled("langchain", "agents", exc) from exc

    try:
        return init_chat_model(model, **kwargs)
    except ImportError as exc:  # the provider integration package is missing
        raise ModelProviderNotInstalled(provider, extra, exc) from exc
