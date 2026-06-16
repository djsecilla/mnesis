"""Tests for the mnesis_agents model factory and config.

All offline: the stub path needs no provider extra and no API key. The
"missing extra" path is exercised by selecting a provider and forcing its
LangChain integration import to fail.
"""
from __future__ import annotations

import builtins
import importlib

import pytest

from mnesis_agents import config as agents_config
from mnesis_agents.models import (
    ModelProviderNotInstalled,
    get_chat_model,
    make_stub_model,
)


# ── stub model ────────────────────────────────────────────────────────────────


def test_stub_model_usable_without_keys(monkeypatch):
    monkeypatch.setattr(agents_config, "MNESIS_AGENTS_STUB", True)
    model = get_chat_model()
    from langchain_core.language_models import BaseChatModel

    assert isinstance(model, BaseChatModel)
    out = model.invoke("anything")
    assert isinstance(out.content, str) and out.content  # deterministic canned reply


def test_stub_is_deterministic_and_cycles():
    m = make_stub_model(["one", "two"])
    got = [m.invoke("x").content for _ in range(5)]
    assert got == ["one", "two", "one", "two", "one"]  # cycles, never exhausts


def test_stub_can_script_tool_calls():
    from langchain_core.messages import AIMessage

    scripted = AIMessage(
        content="",
        tool_calls=[{"name": "mnesis_query", "args": {"q": "redis"}, "id": "t1"}],
    )
    m = make_stub_model([scripted])
    out = m.invoke("go")
    assert out.tool_calls and out.tool_calls[0]["name"] == "mnesis_query"


def test_stub_supports_bind_tools():
    # bind_tools must exist so later agent code can attach Mnesis tools to the stub.
    m = make_stub_model()
    assert hasattr(m, "bind_tools")


# ── config defaults / parsing ─────────────────────────────────────────────────


def test_config_defaults(monkeypatch):
    for var in (
        "MNESIS_LLM_PROVIDER", "MNESIS_LLM_MODEL", "MNESIS_LLM_BASE_URL",
        "MNESIS_LLM_API_KEY", "MNESIS_LLM_TEMPERATURE", "MNESIS_MCP_TOKEN",
        "MNESIS_AGENTS_STUB", "MNESIS_MCP_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    cfg = importlib.reload(agents_config)
    try:
        assert cfg.MNESIS_LLM_PROVIDER == "openai"
        assert cfg.MNESIS_LLM_MODEL is None
        assert cfg.MNESIS_LLM_TEMPERATURE == 0.0
        assert cfg.MNESIS_AGENTS_STUB is False
        assert cfg.MNESIS_MCP_URL == "http://localhost:8080/mcp"
        assert "openai_compatible" in cfg.SUPPORTED_PROVIDERS
    finally:
        importlib.reload(agents_config)  # restore module-level state for other tests


def test_config_parses_env(monkeypatch):
    monkeypatch.setenv("MNESIS_LLM_PROVIDER", "Anthropic")
    monkeypatch.setenv("MNESIS_LLM_MODEL", "claude-sonnet-4-6")
    monkeypatch.setenv("MNESIS_LLM_TEMPERATURE", "0.7")
    monkeypatch.setenv("MNESIS_AGENTS_STUB", "yes")
    cfg = importlib.reload(agents_config)
    try:
        assert cfg.MNESIS_LLM_PROVIDER == "anthropic"  # lowercased
        assert cfg.MNESIS_LLM_MODEL == "claude-sonnet-4-6"
        assert cfg.MNESIS_LLM_TEMPERATURE == 0.7
        assert cfg.MNESIS_AGENTS_STUB is True
    finally:
        importlib.reload(agents_config)


def test_blank_env_is_treated_as_unset(monkeypatch):
    monkeypatch.setenv("MNESIS_LLM_MODEL", "   ")
    cfg = importlib.reload(agents_config)
    try:
        assert cfg.MNESIS_LLM_MODEL is None
    finally:
        importlib.reload(agents_config)


# ── real-model error paths (no network — fail before any provider call) ───────


def test_unknown_provider_raises_clear_error(monkeypatch):
    monkeypatch.setattr(agents_config, "MNESIS_AGENTS_STUB", False)
    with pytest.raises(ValueError, match="Unsupported MNESIS_LLM_PROVIDER"):
        get_chat_model(provider="not_a_provider", model="x")


def test_missing_model_raises_clear_error(monkeypatch):
    monkeypatch.setattr(agents_config, "MNESIS_AGENTS_STUB", False)
    monkeypatch.setattr(agents_config, "MNESIS_LLM_MODEL", None)
    with pytest.raises(ValueError, match="MNESIS_LLM_MODEL is required"):
        get_chat_model(provider="openai")


def test_missing_provider_extra_raises_actionable_error(monkeypatch):
    # Simulate the provider integration package not being installed: make its
    # import fail, and assert we surface an actionable "install the extra" error.
    monkeypatch.setattr(agents_config, "MNESIS_AGENTS_STUB", False)

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("langchain_anthropic"):
            raise ImportError("No module named 'langchain_anthropic'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ModelProviderNotInstalled) as ei:
        get_chat_model(provider="anthropic", model="claude-sonnet-4-6")
    assert ei.value.extra == "agents-anthropic"
    assert "pip install" in str(ei.value)
