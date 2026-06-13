"""Tests for the provider tool-use layer.

All tests run offline:
  - Stub tests use StubProvider directly.
  - Anthropic tests inject a fake _create callable (no network, no API key).
  - Local tests inject a fake _post callable (no network, no Ollama).
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from mnesis_agent.mcp_client import ToolSpec
from mnesis_agent.provider import (
    DEFAULT_SCRIPT,
    AnthropicProvider,
    AssistantMessage,
    AssistantTurn,
    LocalProvider,
    StubProvider,
    ToolCall,
    ToolResult,
    ToolResultMessage,
    UserMessage,
    _messages_to_anthropic,
    _messages_to_openai,
    _spec_to_anthropic_tool,
    _spec_to_openai_tool,
    get_provider,
)


def run(coro):
    return asyncio.run(coro)


# ── Helpers ───────────────────────────────────────────────────────────────────

TOOL_A = ToolSpec(
    name="test_tool",
    description="A test tool",
    input_schema={"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
)


def _anthropic_text(text="done") -> object:
    """Fake Anthropic Message with a text block."""
    block = SimpleNamespace(type="text", text=text)
    usage = SimpleNamespace(input_tokens=10, output_tokens=5)
    return SimpleNamespace(stop_reason="end_turn", content=[block], usage=usage)


def _anthropic_tool_use(tool_id="tc_1", name="test_tool", input_=None) -> object:
    """Fake Anthropic Message with a tool_use block."""
    block = SimpleNamespace(type="tool_use", id=tool_id, name=name, input=input_ or {"x": "val"})
    usage = SimpleNamespace(input_tokens=10, output_tokens=8)
    return SimpleNamespace(stop_reason="tool_use", content=[block], usage=usage)


def _anthropic_mixed(text="here:", tool_id="tc_1", name="test_tool") -> object:
    """Fake Anthropic Message with both a text block and a tool_use block."""
    blocks = [
        SimpleNamespace(type="text", text=text),
        SimpleNamespace(type="tool_use", id=tool_id, name=name, input={"x": "y"}),
    ]
    usage = SimpleNamespace(input_tokens=12, output_tokens=9)
    return SimpleNamespace(stop_reason="tool_use", content=blocks, usage=usage)


def _oai_text(text="done") -> dict:
    """Fake OpenAI /v1/chat/completions response with a text completion."""
    return {
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _oai_tool_call(tool_id="tc_1", name="test_tool", args=None) -> dict:
    """Fake OpenAI /v1/chat/completions response with a function tool call."""
    return {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": tool_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args or {"x": "val"})},
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 8},
    }


# ── StubProvider ──────────────────────────────────────────────────────────────


def test_stub_first_turn_is_tool_call():
    provider = StubProvider()
    turn = run(provider.complete_with_tools("sys", [UserMessage("hello")], [TOOL_A]))
    assert turn.stop_reason == "tool_use"
    assert len(turn.tool_calls) == 1
    tc = turn.tool_calls[0]
    assert tc.id and tc.name and isinstance(tc.args, dict)


def test_stub_second_turn_is_final_answer():
    provider = StubProvider()
    # First call consumes turn 0 (tool call).
    run(provider.complete_with_tools("sys", [UserMessage("hello")], []))
    # Feed tool results back.
    messages = [
        UserMessage("hello"),
        AssistantMessage("", DEFAULT_SCRIPT[0].tool_calls),
        ToolResultMessage([ToolResult(id="stub_tc_0", name="mnesis_query", content='{"hits": []}')]),
    ]
    turn = run(provider.complete_with_tools("sys", messages, []))
    assert turn.stop_reason == "end_turn"
    assert turn.text
    assert turn.tool_calls == []


def test_stub_full_round_trip():
    """Simulate one complete agent loop: call → tool → feed result → answer."""
    provider = StubProvider()

    # Round 1: model asks to call a tool.
    t1 = run(provider.complete_with_tools("sys", [UserMessage("q")], [TOOL_A]))
    assert t1.stop_reason == "tool_use"
    assert t1.tool_calls

    # Round 2: tool result fed back; model gives final answer.
    messages = [
        UserMessage("q"),
        AssistantMessage(t1.text, t1.tool_calls),
        ToolResultMessage([ToolResult(id=tc.id, name=tc.name, content="result") for tc in t1.tool_calls]),
    ]
    t2 = run(provider.complete_with_tools("sys", messages, [TOOL_A]))
    assert t2.stop_reason == "end_turn"
    assert t2.text
    assert t2.tool_calls == []


def test_stub_script_exhausted_returns_sentinel():
    provider = StubProvider(script=[
        AssistantTurn("only turn", [], "end_turn", {"input_tokens": 1, "output_tokens": 1}),
    ])
    run(provider.complete_with_tools("s", [], []))  # consumes the one turn
    sentinel = run(provider.complete_with_tools("s", [], []))
    assert "exhausted" in sentinel.text
    assert sentinel.stop_reason == "end_turn"
    assert sentinel.tool_calls == []


def test_stub_custom_script():
    script = [
        AssistantTurn("step1", [ToolCall("x", "foo", {"a": 1})], "tool_use", {}),
        AssistantTurn("step2", [ToolCall("y", "bar", {"b": 2})], "tool_use", {}),
        AssistantTurn("done", [], "end_turn", {}),
    ]
    provider = StubProvider(script=script)
    t1 = run(provider.complete_with_tools("s", [], []))
    t2 = run(provider.complete_with_tools("s", [], []))
    t3 = run(provider.complete_with_tools("s", [], []))
    assert t1.text == "step1" and t1.tool_calls[0].name == "foo"
    assert t2.text == "step2" and t2.tool_calls[0].name == "bar"
    assert t3.text == "done" and t3.tool_calls == []


def test_stub_reset():
    provider = StubProvider()
    run(provider.complete_with_tools("s", [], []))
    run(provider.complete_with_tools("s", [], []))
    provider.reset()
    t = run(provider.complete_with_tools("s", [], []))
    assert t.stop_reason == "tool_use"  # back to turn 0


def test_stub_usage_fields():
    provider = StubProvider()
    turn = run(provider.complete_with_tools("s", [], []))
    assert "input_tokens" in turn.usage
    assert "output_tokens" in turn.usage
    assert isinstance(turn.usage["input_tokens"], int)


# ── Anthropic schema mapping ──────────────────────────────────────────────────


def test_spec_to_anthropic_tool_shape():
    d = _spec_to_anthropic_tool(TOOL_A)
    assert d["name"] == "test_tool"
    assert d["description"] == "A test tool"
    assert d["input_schema"]["type"] == "object"
    assert "x" in d["input_schema"]["properties"]


def test_spec_to_anthropic_tool_empty_schema_gets_object():
    d = _spec_to_anthropic_tool(ToolSpec("t", "desc"))
    assert d["input_schema"] == {"type": "object"}


def test_messages_to_anthropic_user():
    result = _messages_to_anthropic([UserMessage("hello")])
    assert result == [{"role": "user", "content": "hello"}]


def test_messages_to_anthropic_assistant_text_only():
    result = _messages_to_anthropic([AssistantMessage("hi there")])
    assert result == [{"role": "assistant", "content": "hi there"}]


def test_messages_to_anthropic_assistant_with_tool_calls():
    msg = AssistantMessage(
        text="thinking…",
        tool_calls=[ToolCall(id="tc_1", name="foo", args={"a": 1})],
    )
    result = _messages_to_anthropic([msg])
    assert len(result) == 1
    content = result[0]["content"]
    assert isinstance(content, list)
    types = {b["type"] for b in content}
    assert "text" in types and "tool_use" in types
    tu = next(b for b in content if b["type"] == "tool_use")
    assert tu["id"] == "tc_1" and tu["name"] == "foo" and tu["input"] == {"a": 1}


def test_messages_to_anthropic_assistant_no_text_only_tool_call():
    msg = AssistantMessage(text="", tool_calls=[ToolCall(id="tc_1", name="foo", args={})])
    result = _messages_to_anthropic([msg])
    content = result[0]["content"]
    # No text block — only the tool_use block.
    assert all(b["type"] == "tool_use" for b in content)


def test_messages_to_anthropic_tool_result():
    msg = ToolResultMessage([
        ToolResult(id="tc_1", name="foo", content='{"ok": true}'),
    ])
    result = _messages_to_anthropic([msg])
    assert result[0]["role"] == "user"
    blocks = result[0]["content"]
    assert blocks[0]["type"] == "tool_result"
    assert blocks[0]["tool_use_id"] == "tc_1"
    assert blocks[0]["content"] == '{"ok": true}'
    assert "is_error" not in blocks[0]


def test_messages_to_anthropic_tool_result_error_flag():
    msg = ToolResultMessage([ToolResult(id="tc_1", name="foo", content="err", is_error=True)])
    result = _messages_to_anthropic([msg])
    assert result[0]["content"][0]["is_error"] is True


def test_messages_to_anthropic_multi_turn_sequence():
    messages = [
        UserMessage("query redis"),
        AssistantMessage("", [ToolCall("tc_1", "mnesis_query", {"query": "redis"})]),
        ToolResultMessage([ToolResult("tc_1", "mnesis_query", '{"hits": []}')]),
    ]
    result = _messages_to_anthropic(messages)
    assert len(result) == 3
    assert result[0]["role"] == "user"
    assert result[1]["role"] == "assistant"
    assert result[2]["role"] == "user"
    assert result[2]["content"][0]["type"] == "tool_result"


def test_anthropic_provider_text_response():
    async def fake_create(**kwargs):
        return _anthropic_text("The answer is 42.")

    provider = AnthropicProvider("model", _create=fake_create)
    turn = run(provider.complete_with_tools("sys", [UserMessage("q")], []))
    assert turn.text == "The answer is 42."
    assert turn.tool_calls == []
    assert turn.stop_reason == "end_turn"


def test_anthropic_provider_tool_use_response():
    async def fake_create(**kwargs):
        return _anthropic_tool_use()

    provider = AnthropicProvider("model", _create=fake_create)
    turn = run(provider.complete_with_tools("sys", [UserMessage("q")], [TOOL_A]))
    assert turn.stop_reason == "tool_use"
    assert len(turn.tool_calls) == 1
    tc = turn.tool_calls[0]
    assert tc.id == "tc_1" and tc.name == "test_tool" and tc.args == {"x": "val"}


def test_anthropic_provider_mixed_text_and_tool_call():
    async def fake_create(**kwargs):
        return _anthropic_mixed(text="I'll look that up:", tool_id="tc_2")

    provider = AnthropicProvider("model", _create=fake_create)
    turn = run(provider.complete_with_tools("sys", [UserMessage("q")], [TOOL_A]))
    assert turn.text == "I'll look that up:"
    assert len(turn.tool_calls) == 1
    assert turn.tool_calls[0].id == "tc_2"


def test_anthropic_provider_sends_tools_in_kwargs():
    captured: dict = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return _anthropic_text()

    provider = AnthropicProvider("my-model", _create=fake_create)
    run(provider.complete_with_tools("sys", [UserMessage("q")], [TOOL_A]))
    assert "tools" in captured
    assert captured["tools"][0]["name"] == "test_tool"
    assert captured["model"] == "my-model"


def test_anthropic_provider_no_tools_omits_tools_key():
    captured: dict = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return _anthropic_text()

    provider = AnthropicProvider("model", _create=fake_create)
    run(provider.complete_with_tools("sys", [UserMessage("q")], []))
    assert "tools" not in captured


def test_anthropic_provider_usage_in_turn():
    async def fake_create(**kwargs):
        return _anthropic_text()

    provider = AnthropicProvider("model", _create=fake_create)
    turn = run(provider.complete_with_tools("sys", [UserMessage("q")], []))
    assert turn.usage["input_tokens"] == 10
    assert turn.usage["output_tokens"] == 5


# ── Local schema mapping ──────────────────────────────────────────────────────


def test_spec_to_openai_tool_shape():
    d = _spec_to_openai_tool(TOOL_A)
    assert d["type"] == "function"
    fn = d["function"]
    assert fn["name"] == "test_tool"
    assert fn["description"] == "A test tool"
    assert fn["parameters"]["type"] == "object"
    assert "x" in fn["parameters"]["properties"]


def test_spec_to_openai_tool_empty_schema_gets_object():
    d = _spec_to_openai_tool(ToolSpec("t", "desc"))
    assert d["function"]["parameters"] == {"type": "object"}


def test_messages_to_openai_user():
    result = _messages_to_openai([UserMessage("hello")])
    assert result == [{"role": "user", "content": "hello"}]


def test_messages_to_openai_assistant_text_only():
    result = _messages_to_openai([AssistantMessage("hi")])
    assert result[0] == {"role": "assistant", "content": "hi"}
    assert "tool_calls" not in result[0]


def test_messages_to_openai_assistant_with_tool_calls():
    msg = AssistantMessage("", [ToolCall("tc_1", "foo", {"a": 1})])
    result = _messages_to_openai([msg])
    m = result[0]
    assert m["role"] == "assistant"
    assert m["tool_calls"][0]["id"] == "tc_1"
    assert m["tool_calls"][0]["type"] == "function"
    assert m["tool_calls"][0]["function"]["name"] == "foo"
    # arguments must be a JSON string
    assert json.loads(m["tool_calls"][0]["function"]["arguments"]) == {"a": 1}


def test_messages_to_openai_tool_result_expands_to_multiple_messages():
    msg = ToolResultMessage([
        ToolResult("tc_1", "foo", '{"r": 1}'),
        ToolResult("tc_2", "bar", '{"r": 2}'),
    ])
    result = _messages_to_openai([msg])
    assert len(result) == 2
    for r in result:
        assert r["role"] == "tool"
        assert "tool_call_id" in r and "name" in r and "content" in r


def test_messages_to_openai_multi_turn_sequence():
    messages = [
        UserMessage("q"),
        AssistantMessage("", [ToolCall("tc_1", "mnesis_query", {"query": "x"})]),
        ToolResultMessage([ToolResult("tc_1", "mnesis_query", '{}')]),
    ]
    result = _messages_to_openai(messages)
    assert result[0]["role"] == "user"
    assert result[1]["role"] == "assistant"
    assert result[2]["role"] == "tool" and result[2]["tool_call_id"] == "tc_1"


def test_local_provider_text_response():
    provider = LocalProvider("model", "http://localhost:11434", _post=lambda u, p: _oai_text("Looks good."))
    turn = run(provider.complete_with_tools("sys", [UserMessage("q")], []))
    assert turn.text == "Looks good."
    assert turn.tool_calls == []
    assert turn.stop_reason == "end_turn"


def test_local_provider_tool_call_response():
    provider = LocalProvider("model", "http://localhost:11434",
                             _post=lambda u, p: _oai_tool_call("tc_1", "test_tool", {"x": "val"}))
    turn = run(provider.complete_with_tools("sys", [UserMessage("q")], [TOOL_A]))
    assert turn.stop_reason == "tool_use"
    assert len(turn.tool_calls) == 1
    tc = turn.tool_calls[0]
    assert tc.id == "tc_1" and tc.name == "test_tool" and tc.args == {"x": "val"}


def test_local_provider_tool_call_stop_override():
    # Some Ollama models return finish_reason="stop" even when emitting tool calls.
    resp = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "tc_1", "type": "function",
                                "function": {"name": "foo", "arguments": "{}"}}],
            },
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3},
    }
    provider = LocalProvider("model", "http://localhost:11434", _post=lambda u, p: resp)
    turn = run(provider.complete_with_tools("sys", [UserMessage("q")], [TOOL_A]))
    assert turn.stop_reason == "tool_use"  # overridden because tool_calls present


def test_local_provider_sends_tools_in_payload():
    captured: dict = {}

    def fake_post(url, payload):
        captured.update(payload)
        return _oai_text()

    provider = LocalProvider("my-model", "http://localhost:11434", _post=fake_post)
    run(provider.complete_with_tools("sys", [UserMessage("q")], [TOOL_A]))
    assert "tools" in captured
    assert captured["tools"][0]["type"] == "function"
    assert captured["tools"][0]["function"]["name"] == "test_tool"
    assert captured["model"] == "my-model"


def test_local_provider_no_tools_omits_tools_key():
    captured: dict = {}

    def fake_post(url, payload):
        captured.update(payload)
        return _oai_text()

    provider = LocalProvider("model", "http://localhost:11434", _post=fake_post)
    run(provider.complete_with_tools("sys", [UserMessage("q")], []))
    assert "tools" not in captured


def test_local_provider_system_message_prepended():
    captured: dict = {}

    def fake_post(url, payload):
        captured.update(payload)
        return _oai_text()

    provider = LocalProvider("model", "http://localhost:11434", _post=fake_post)
    run(provider.complete_with_tools("SYSTEM", [UserMessage("q")], []))
    msgs = captured["messages"]
    assert msgs[0]["role"] == "system" and msgs[0]["content"] == "SYSTEM"
    assert msgs[1]["role"] == "user"


def test_local_provider_usage_mapping():
    provider = LocalProvider("model", "http://localhost:11434",
                             _post=lambda u, p: _oai_text())
    turn = run(provider.complete_with_tools("sys", [UserMessage("q")], []))
    assert turn.usage["input_tokens"] == 10
    assert turn.usage["output_tokens"] == 5


def test_local_provider_arguments_json_string_parsed():
    # arguments as a JSON string (standard) should parse to dict.
    resp = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "tc_x", "type": "function",
                                "function": {"name": "foo", "arguments": '{"key": "val"}'}}],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {},
    }
    provider = LocalProvider("model", "http://localhost:11434", _post=lambda u, p: resp)
    turn = run(provider.complete_with_tools("sys", [UserMessage("q")], [TOOL_A]))
    assert turn.tool_calls[0].args == {"key": "val"}


# ── Factory ───────────────────────────────────────────────────────────────────


def test_get_provider_returns_stub_when_stub_flag(monkeypatch):
    import mnesis_agent.config as ac
    monkeypatch.setattr(ac, "MNESIS_LLM_STUB", True)
    import mnesis_agent.provider as ap
    monkeypatch.setattr(ap.config, "MNESIS_LLM_STUB", True)
    p = get_provider()
    assert isinstance(p, StubProvider)


def test_get_provider_returns_local_when_provider_local(monkeypatch):
    import mnesis_agent.provider as ap
    monkeypatch.setattr(ap.config, "MNESIS_LLM_STUB", False)
    monkeypatch.setattr(ap.config, "MNESIS_LLM_PROVIDER", "local")
    p = get_provider()
    assert isinstance(p, LocalProvider)


def test_get_provider_returns_anthropic_by_default(monkeypatch):
    import mnesis_agent.provider as ap
    monkeypatch.setattr(ap.config, "MNESIS_LLM_STUB", False)
    monkeypatch.setattr(ap.config, "MNESIS_LLM_PROVIDER", "anthropic")
    p = get_provider()
    assert isinstance(p, AnthropicProvider)
