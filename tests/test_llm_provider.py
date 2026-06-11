"""Tests for the LLM provider switch (anthropic | local) — offline, mocked."""

from __future__ import annotations

import httpx
import pytest

from mnesis import config, llm


def test_stub_flag_is_provider_aware(monkeypatch):
    monkeypatch.delenv("MNESIS_LLM_STUB", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    # Anthropic + no key -> stub (offline default).
    monkeypatch.setenv("MNESIS_LLM_PROVIDER", "anthropic")
    assert config._read_stub_flag() is True

    # Local + no key -> NOT stub (it has its own endpoint).
    monkeypatch.setenv("MNESIS_LLM_PROVIDER", "local")
    assert config._read_stub_flag() is False

    # Explicit stub wins regardless of provider.
    monkeypatch.setenv("MNESIS_LLM_STUB", "1")
    assert config._read_stub_flag() is True


def test_local_provider_calls_openai_compatible_endpoint(monkeypatch):
    monkeypatch.setattr(config, "MNESIS_LLM_STUB", False)
    monkeypatch.setattr(config, "MNESIS_LLM_PROVIDER", "local")
    monkeypatch.setattr(config, "MNESIS_LLM_BASE_URL", "http://ollama:11434")
    monkeypatch.setattr(config, "MNESIS_LLM_MODEL", "llama3.2:1b")

    seen = {}

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "local-result"}}]}

    def _fake_post(url, json, timeout):
        seen["url"] = url
        seen["model"] = json["model"]
        seen["roles"] = [m["role"] for m in json["messages"]]
        return _FakeResponse()

    monkeypatch.setattr(httpx, "post", _fake_post)

    out = llm.complete("system prompt", "user prompt")
    assert out == "local-result"
    assert seen["url"] == "http://ollama:11434/v1/chat/completions"
    assert seen["model"] == "llama3.2:1b"
    assert seen["roles"] == ["system", "user"]


def test_local_provider_makes_no_anthropic_call(monkeypatch):
    monkeypatch.setattr(config, "MNESIS_LLM_STUB", False)
    monkeypatch.setattr(config, "MNESIS_LLM_PROVIDER", "local")

    def _boom(*a, **k):
        raise AssertionError("the Anthropic path must not be used in local mode")

    monkeypatch.setattr(llm, "_anthropic_complete", _boom)
    monkeypatch.setattr(
        httpx, "post",
        lambda *a, **k: type("R", (), {
            "raise_for_status": lambda self: None,
            "json": lambda self: {"choices": [{"message": {"content": "ok"}}]},
        })(),
    )
    assert llm.complete("s", "u") == "ok"


def test_stub_takes_precedence_over_local(monkeypatch):
    monkeypatch.setattr(config, "MNESIS_LLM_STUB", True)
    monkeypatch.setattr(config, "MNESIS_LLM_PROVIDER", "local")

    def _boom(*a, **k):
        raise AssertionError("stub must short-circuit before any provider call")

    monkeypatch.setattr(llm, "_local_complete", _boom)
    # Stub returns deterministic extraction JSON, not a provider call.
    assert '"title"' in llm.complete("s", "Atlas uses Redis.")
