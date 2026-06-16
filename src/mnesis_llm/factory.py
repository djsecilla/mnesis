"""Provider-agnostic chat-model factory (shared by mnesis core + mnesis_agents).

The single place that maps a provider key to a LangChain integration. Pure: it
takes explicit args (no config import), so each layer passes its own env-resolved
values. langchain is imported lazily — importing this module needs nothing extra,
and a missing provider package raises an actionable :class:`ModelProviderNotInstalled`.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # type-only — no runtime import
    from langchain_core.language_models import BaseChatModel

#: Provider keys accepted across the whole system (Mnesis + agents).
SUPPORTED_PROVIDERS: tuple[str, ...] = (
    "openai",
    "anthropic",
    "google",
    "mistral",
    "bedrock",
    "ollama",
    "openai_compatible",
)

#: provider -> (init_chat_model model_provider, install-extra name).
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
            f'Install it with:  pip install -e ".[{extra}]"  '
            f'(or `uv pip install -e ".[{extra}]"`).'
            + (f"\nUnderlying import error: {cause}" if cause else "")
        )


def make_stub_model(responses: list[Any] | None = None) -> "BaseChatModel":
    """A deterministic, offline fake chat model — no keys, no network.

    Cycles through ``responses`` (strings or pre-built ``AIMessage``s, the latter
    for scripting tool-calls) forever. Overrides ``bind_tools`` (the base raises
    NotImplementedError) so it can drive the agent loop offline.
    """
    from itertools import cycle

    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    class _StubChatModel(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ARG002
            return self  # stub ignores tools; scripted messages drive behaviour

    items = responses or ["This is a deterministic mnesis stub response."]
    messages = [m if isinstance(m, AIMessage) else AIMessage(content=str(m)) for m in items]
    return _StubChatModel(messages=cycle(messages))


def get_chat_model(
    provider: str,
    model: str,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    temperature: float = 0.0,
) -> "BaseChatModel":
    """Build a LangChain chat model for ``provider``/``model``.

    Caller is expected to have validated provider/model (so env-specific error
    messages live in the caller). Raises ValueError for an unmapped provider and
    :class:`ModelProviderNotInstalled` when the provider package is missing.
    """
    if provider not in _PROVIDER_MAP:
        raise ValueError(f"unsupported provider {provider!r}; one of {', '.join(SUPPORTED_PROVIDERS)}")
    lc_provider, extra = _PROVIDER_MAP[provider]

    kwargs: dict[str, Any] = {"model_provider": lc_provider, "temperature": temperature}
    if base_url:
        kwargs["base_url"] = base_url
    if api_key:
        kwargs["api_key"] = api_key

    try:
        from langchain.chat_models import init_chat_model
    except ImportError as exc:
        raise ModelProviderNotInstalled("langchain", "agents", exc) from exc
    try:
        return init_chat_model(model, **kwargs)
    except ImportError as exc:
        raise ModelProviderNotInstalled(provider, extra, exc) from exc
