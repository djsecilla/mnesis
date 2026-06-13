"""Provider-agnostic tool-use completion for the agent loop.

Public interface
----------------
complete_with_tools(system, messages, tools) -> AssistantTurn
get_provider() -> Provider   # from env config

Neutral message types (never leaking provider-specific shapes):
  UserMessage(content)
  AssistantMessage(text, tool_calls)
  ToolResultMessage(results)

ToolCall(id, name, args)          -- from the model; fed to the tool source
ToolResult(id, name, content)     -- from executing the tool; fed back to model
AssistantTurn(text, tool_calls, stop_reason, usage)  -- returned each round

Three providers:
  StubProvider    -- scripted, deterministic, no network needed (offline tests)
  AnthropicProvider -- Anthropic Messages API with tool_use blocks
  LocalProvider   -- Ollama/OpenAI-compatible /v1/chat/completions with functions

Provider differences (message serialisation, schema mapping, stop-reason names)
stay entirely inside each adapter.  The agent loop never sees them.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Union

from . import config
from .mcp_client import ToolSpec


# ── Neutral data types ────────────────────────────────────────────────────────


@dataclass
class ToolCall:
    """A tool call emitted by the model in one turn."""

    id: str    # opaque, round-trips through ToolResult to pair the result
    name: str  # tool name
    args: dict  # parsed arguments dict


@dataclass
class ToolResult:
    """The result of executing one ToolCall, to be fed back to the model."""

    id: str          # must match the ToolCall.id
    name: str        # tool name (required by OpenAI; informational for Anthropic)
    content: str     # result as a string (typically JSON)
    is_error: bool = False


@dataclass
class UserMessage:
    content: str


@dataclass
class AssistantMessage:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass
class ToolResultMessage:
    """One or more results that answer the assistant's previous tool calls."""

    results: list[ToolResult]


#: Union of all neutral message types accepted by complete_with_tools.
Message = Union[UserMessage, AssistantMessage, ToolResultMessage]


@dataclass
class AssistantTurn:
    """The model's response for one round of the agent loop."""

    text: str               # generated text (may be empty when tools are called)
    tool_calls: list[ToolCall]   # empty if stop_reason != "tool_use"
    stop_reason: str        # "end_turn" | "tool_use" | "max_tokens"
    usage: dict             # {"input_tokens": int, "output_tokens": int}


# ── Provider ABC ──────────────────────────────────────────────────────────────


class Provider(ABC):
    @abstractmethod
    async def complete_with_tools(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> AssistantTurn: ...


# ── Stub provider — scripted, offline ─────────────────────────────────────────

#: Default two-turn script used when no custom script is passed.
#: Turn 0: calls mnesis_query (simulates the model choosing to look something up).
#: Turn 1: final text answer (simulates the model synthesising from the result).
DEFAULT_SCRIPT: list[AssistantTurn] = [
    AssistantTurn(
        text="",
        tool_calls=[ToolCall(id="stub_tc_0", name="mnesis_query", args={"query": "stub query"})],
        stop_reason="tool_use",
        usage={"input_tokens": 10, "output_tokens": 5},
    ),
    AssistantTurn(
        text="Stub answer from the knowledge base.",
        tool_calls=[],
        stop_reason="end_turn",
        usage={"input_tokens": 20, "output_tokens": 10},
    ),
]


class StubProvider(Provider):
    """Deterministic scripted provider — no API key, no network.

    Each call to complete_with_tools pops the next AssistantTurn from the
    script.  Once the script is exhausted every subsequent call returns a
    sentinel "(stub: script exhausted)" turn so tests can detect overruns.

    Pass a custom ``script`` to drive arbitrary agent-loop sequences offline.
    Call ``reset()`` to replay the same script in a second test.
    """

    def __init__(self, script: list[AssistantTurn] | None = None) -> None:
        self._script = list(script if script is not None else DEFAULT_SCRIPT)
        self._pos = 0

    async def complete_with_tools(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> AssistantTurn:
        if self._pos < len(self._script):
            turn = self._script[self._pos]
            self._pos += 1
            return turn
        return AssistantTurn(
            text="(stub: script exhausted)",
            tool_calls=[],
            stop_reason="end_turn",
            usage={"input_tokens": 0, "output_tokens": 0},
        )

    def reset(self) -> None:
        """Rewind the script so it can be replayed."""
        self._pos = 0


# ── Anthropic adapter ─────────────────────────────────────────────────────────


def _spec_to_anthropic_tool(spec: ToolSpec) -> dict:
    """Map a ToolSpec to an Anthropic ToolParam dict."""
    return {
        "name": spec.name,
        "description": spec.description,
        "input_schema": spec.input_schema or {"type": "object"},
    }


def _messages_to_anthropic(messages: list[Message]) -> list[dict]:
    """Convert neutral messages to the Anthropic Messages API format."""
    out: list[dict] = []
    for msg in messages:
        if isinstance(msg, UserMessage):
            out.append({"role": "user", "content": msg.content})

        elif isinstance(msg, AssistantMessage):
            if not msg.tool_calls:
                # Plain text turn — string content is fine.
                out.append({"role": "assistant", "content": msg.text})
            else:
                # Mixed content: optional text block + one tool_use block per call.
                content: list = []
                if msg.text:
                    content.append({"type": "text", "text": msg.text})
                for tc in msg.tool_calls:
                    content.append(
                        {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.args}
                    )
                out.append({"role": "assistant", "content": content})

        elif isinstance(msg, ToolResultMessage):
            # Tool results go back as a "user" message with tool_result blocks.
            out.append({
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": r.id,
                        "content": r.content,
                        **({"is_error": True} if r.is_error else {}),
                    }
                    for r in msg.results
                ],
            })
    return out


class AnthropicProvider(Provider):
    """Anthropic Messages API adapter with tool_use support.

    ``_create`` is an optional async callable matching the signature of
    ``anthropic.AsyncAnthropic().messages.create``.  Inject a fake for tests
    — avoids any network call while testing the full mapping logic.
    """

    MAX_TOKENS = 1024

    def __init__(self, model: str, *, _create=None) -> None:
        self._model = model
        self._override_create = _create
        self._client = None  # lazily constructed real async client

    def _get_create(self):
        if self._override_create is not None:
            return self._override_create
        if self._client is None:
            import anthropic
            self._client = anthropic.AsyncAnthropic()
        return self._client.messages.create

    async def complete_with_tools(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> AssistantTurn:
        kwargs: dict = dict(
            model=self._model,
            max_tokens=self.MAX_TOKENS,
            system=system,
            messages=_messages_to_anthropic(messages),
        )
        if tools:
            kwargs["tools"] = [_spec_to_anthropic_tool(t) for t in tools]

        response = await self._get_create()(**kwargs)

        text = ""
        tool_calls: list[ToolCall] = []
        for block in response.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                text += block.text
            elif btype == "tool_use":
                tool_calls.append(
                    ToolCall(id=block.id, name=block.name, args=dict(block.input))
                )

        return AssistantTurn(
            text=text,
            tool_calls=tool_calls,
            stop_reason=response.stop_reason or "end_turn",
            usage={
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
        )


# ── Local / Ollama adapter ────────────────────────────────────────────────────


def _spec_to_openai_tool(spec: ToolSpec) -> dict:
    """Map a ToolSpec to an OpenAI-compatible function tool dict."""
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.input_schema or {"type": "object"},
        },
    }


def _messages_to_openai(messages: list[Message]) -> list[dict]:
    """Convert neutral messages to the OpenAI /v1/chat/completions format.

    ToolResultMessage expands to one ``{"role": "tool"}`` message per result
    (the OpenAI convention — separate message per tool call).
    """
    out: list[dict] = []
    for msg in messages:
        if isinstance(msg, UserMessage):
            out.append({"role": "user", "content": msg.content})

        elif isinstance(msg, AssistantMessage):
            m: dict = {"role": "assistant", "content": msg.text or ""}
            if msg.tool_calls:
                m["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.args),
                        },
                    }
                    for tc in msg.tool_calls
                ]
            out.append(m)

        elif isinstance(msg, ToolResultMessage):
            for r in msg.results:
                out.append({
                    "role": "tool",
                    "tool_call_id": r.id,
                    "name": r.name,
                    "content": r.content,
                })
    return out


# OpenAI finish_reason → neutral stop_reason
_OAI_STOP_REASON = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
}


class LocalProvider(Provider):
    """Ollama / OpenAI-compatible /v1/chat/completions adapter with function calling.

    ``_post`` is an optional sync callable ``(url: str, payload: dict) -> dict``
    that returns the parsed JSON response body.  Inject it in tests to avoid
    any network call while testing the full mapping logic.
    """

    MAX_TOKENS = 1024

    def __init__(self, model: str, base_url: str, *, _post=None) -> None:
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._post = _post  # optional sync override: (url, payload) -> dict

    async def _do_post(self, url: str, payload: dict) -> dict:
        if self._post is not None:
            return self._post(url, payload)
        import httpx
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(url, json=payload)
            r.raise_for_status()
            return r.json()

    async def complete_with_tools(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> AssistantTurn:
        payload: dict = {
            "model": self._model,
            "messages": [{"role": "system", "content": system}] + _messages_to_openai(messages),
            "temperature": 0,
            "stream": False,
        }
        if tools:
            payload["tools"] = [_spec_to_openai_tool(t) for t in tools]

        data = await self._do_post(self._base_url + "/v1/chat/completions", payload)

        choice = data["choices"][0]
        msg = choice["message"]
        finish_reason = choice.get("finish_reason", "stop")

        text = msg.get("content") or ""
        tool_calls: list[ToolCall] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc["function"]
            raw_args = fn["arguments"]
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            tool_calls.append(ToolCall(id=tc["id"], name=fn["name"], args=args))

        # Some models return finish_reason="stop" even when emitting tool_calls.
        stop_reason = _OAI_STOP_REASON.get(finish_reason, finish_reason)
        if tool_calls and stop_reason != "tool_use":
            stop_reason = "tool_use"

        usage_raw = data.get("usage", {})
        return AssistantTurn(
            text=text,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            usage={
                "input_tokens": usage_raw.get("prompt_tokens", 0),
                "output_tokens": usage_raw.get("completion_tokens", 0),
            },
        )


# ── Factory ───────────────────────────────────────────────────────────────────


def get_provider() -> Provider:
    """Return the configured Provider from environment variables.

    Priority: stub flag > MNESIS_LLM_PROVIDER=local > Anthropic (default).
    """
    if config.MNESIS_LLM_STUB:
        return StubProvider()
    if config.MNESIS_LLM_PROVIDER == "local":
        return LocalProvider(config.MNESIS_LLM_MODEL, config.MNESIS_LLM_BASE_URL)
    return AnthropicProvider(config.MNESIS_LLM_MODEL)
