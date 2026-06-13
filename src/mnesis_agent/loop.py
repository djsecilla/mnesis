"""Bounded tool-use agent loop.

run_agent(profile, user_input, tools, provider, registry) -> AgentResult

The agent alternates: reason → call tools → observe → repeat → answer.
Every exit path — normal and guardrail — is safe and returns an AgentResult.

Guardrails (all enforced before every tool-execution round):
  max_iterations   max rounds of tool calls (each LLM turn that returns tool_use)
  max_tool_calls   total individual tool-call dispatches across all rounds
  token_budget     cumulative input-token usage across all LLM calls
  deadline         wall-clock monotonic time limit (timeout_seconds in profile)
  no_progress      repeated (name, args) signature within a sliding window

Tool errors NEVER crash the loop — they are fed back to the model as
tool_result messages with is_error=True so the model can acknowledge and
continue or conclude.
"""
from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from .mcp_client import ToolSpec
from .provider import (
    AssistantMessage,
    Provider,
    ToolCall,
    ToolResult,
    ToolResultMessage,
    UserMessage,
)
from .registry import ToolRegistry


# ── Profile ───────────────────────────────────────────────────────────────────


@dataclass
class AgentProfile:
    """Runtime configuration for one agent run.

    All budget fields have safe defaults that prevent runaway behaviour
    even when the caller omits them.
    """

    system: str
    max_iterations: int = 10        # max rounds of tool calls (LLM turns returning tool_use)
    max_tool_calls: int = 30        # max total individual tool-call dispatches
    max_input_tokens: int = 50_000  # cumulative input-token budget across all LLM calls
    timeout_seconds: float | None = None  # wall-clock budget; None = no limit
    no_progress_window: int = 6     # look-back window for repeated (name, args) detection


# ── Transcript ────────────────────────────────────────────────────────────────


@dataclass
class ThoughtStep:
    """Records one LLM turn: the model's text and any tool calls it requested."""

    turn: int
    text: str
    tool_calls: list[ToolCall]
    kind: str = "thought"  # discriminator for serialisation


@dataclass
class ToolStep:
    """Records one tool dispatch: the call, result, and whether it errored."""

    turn: int
    tool_name: str
    call_id: str
    args: dict
    result: str
    is_error: bool
    kind: str = "tool"  # discriminator for serialisation


TranscriptEntry = ThoughtStep | ToolStep


# ── Result ────────────────────────────────────────────────────────────────────


@dataclass
class AgentResult:
    """The structured outcome of one agent run.

    ``stop_reason`` values:
      "end_turn"       model gave a final answer normally
      "max_iterations" iteration guardrail fired
      "max_tool_calls" per-call guardrail fired
      "token_budget"   input-token budget exhausted
      "deadline"       wall-clock timeout exceeded
      "no_progress"    repeated identical (tool, args) pair detected
      "max_tokens"     provider hit its output-token limit mid-turn
    """

    final_text: str                  # model's last generated text (may be empty on guardrail stops)
    transcript: list[TranscriptEntry]
    tools_used: list[str]            # distinct tool names that were dispatched, sorted
    citations: list[str]             # page IDs found in tool results, unique, order-preserving
    writes: list[ToolCall]           # tool calls whose name is in write_tool_names
    stop_reason: str
    usage: dict                      # total {"input_tokens": int, "output_tokens": int}
    iterations: int                  # tool-use rounds that completed


# ── Citation extractor (default) ──────────────────────────────────────────────


def _extract_citations(tool_name: str, result: str) -> list[str]:
    """Best-effort extraction of page IDs from a JSON tool result.

    Handles the common shapes produced by the mnesis_* tools:
      {"id": "..."}           — mnesis_get, mnesis_ingest page_id, mnesis_file_back digest_id
      {"hits": [{"id": "..."}]}  — mnesis_query
    This is a heuristic; the caller can replace it with a domain-specific one.
    """
    try:
        data = json.loads(result)
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    ids: list[str] = []
    for key in ("id", "page_id", "digest_id"):
        v = data.get(key)
        if isinstance(v, str) and v:
            ids.append(v)
    for hit in data.get("hits", []):
        if isinstance(hit, dict):
            v = hit.get("id")
            if isinstance(v, str) and v:
                ids.append(v)
    return ids


# ── Main loop ─────────────────────────────────────────────────────────────────


async def run_agent(
    profile: AgentProfile,
    user_input: str,
    tools: list[ToolSpec],
    provider: Provider,
    registry: ToolRegistry,
    *,
    write_tool_names: set[str] | None = None,
    citation_extractor: Callable[[str, str], list[str]] | None = _extract_citations,
    audit_hook: Callable[[dict], None] | None = None,
) -> AgentResult:
    """Run the bounded tool-use agent loop.

    Parameters
    ----------
    profile:
        Runtime budgets and the system prompt.
    user_input:
        The user's question or instruction; becomes the first UserMessage.
    tools:
        ToolSpec list passed to the LLM (so it knows what it may call).
    provider:
        LLM provider (Anthropic, Local, or Stub).
    registry:
        Tool dispatch — routes tool_calls to the owning ToolSource.
    write_tool_names:
        Tool names considered "writes" (tracked separately in result.writes).
        Typically: {"mnesis_ingest", "mnesis_file_back"}.
    citation_extractor:
        Called with (tool_name, result_text) → list[page_id].
        Defaults to a JSON heuristic. Pass None to disable.
    audit_hook:
        Async-safe callable invoked for every thought and tool step.
        Receives a dict; A6 will hook persistence here.
    """
    deadline = (
        time.monotonic() + profile.timeout_seconds
        if profile.timeout_seconds is not None
        else None
    )

    messages: list = [UserMessage(user_input)]
    transcript: list[TranscriptEntry] = []
    tools_used: set[str] = set()
    citations: list[str] = []
    citations_set: set[str] = set()  # for O(1) dedup
    writes: list[ToolCall] = []
    total_usage: dict = {"input_tokens": 0, "output_tokens": 0}
    iterations: int = 0
    total_tool_calls: int = 0
    seen_sigs: deque[str] = deque(maxlen=profile.no_progress_window)
    stop_reason = "end_turn"
    final_text = ""

    while True:

        # ── Guardrail: wall-clock deadline ───────────────────────────────────
        if deadline is not None and time.monotonic() > deadline:
            stop_reason = "deadline"
            break

        # ── LLM call ─────────────────────────────────────────────────────────
        turn = await provider.complete_with_tools(profile.system, messages, tools)

        total_usage["input_tokens"] += turn.usage.get("input_tokens", 0)
        total_usage["output_tokens"] += turn.usage.get("output_tokens", 0)
        final_text = turn.text  # always track; overwritten each round

        thought = ThoughtStep(
            turn=iterations,
            text=turn.text,
            tool_calls=list(turn.tool_calls),
        )
        transcript.append(thought)

        if audit_hook:
            audit_hook({
                "kind": "thought",
                "turn": iterations,
                "text_length": len(turn.text),
                "tool_count": len(turn.tool_calls),
                "stop_reason": turn.stop_reason,
            })

        # ── Normal stop — model gave a final answer ───────────────────────────
        if turn.stop_reason != "tool_use" or not turn.tool_calls:
            stop_reason = turn.stop_reason if turn.stop_reason else "end_turn"
            break

        # ── Guardrail: max iterations ─────────────────────────────────────────
        if iterations >= profile.max_iterations:
            stop_reason = "max_iterations"
            break

        # ── Guardrail: input-token budget ─────────────────────────────────────
        if total_usage["input_tokens"] > profile.max_input_tokens:
            stop_reason = "token_budget"
            break

        # ── Execute tool calls for this round ─────────────────────────────────
        results: list[ToolResult] = []
        guardrail_hit = False

        for tc in turn.tool_calls:

            # Guardrail: max total tool calls
            if total_tool_calls >= profile.max_tool_calls:
                stop_reason = "max_tool_calls"
                guardrail_hit = True
                break

            # Guardrail: no-progress — same (tool, args) seen recently
            sig = f"{tc.name}::{json.dumps(tc.args, sort_keys=True)}"
            if sig in seen_sigs:
                stop_reason = "no_progress"
                guardrail_hit = True
                break
            seen_sigs.append(sig)

            total_tool_calls += 1
            tools_used.add(tc.name)

            if write_tool_names and tc.name in write_tool_names:
                writes.append(tc)

            # Dispatch — errors become tool results; loop never crashes
            try:
                result_text = await registry.dispatch(tc.name, tc.args)
                is_error = False
                result_status = "ok"
            except Exception as exc:
                result_text = f"Tool error ({tc.name}): {exc}"
                is_error = True
                result_status = "error"

            # Citation extraction
            if citation_extractor:
                for cid in citation_extractor(tc.name, result_text):
                    if cid not in citations_set:
                        citations.append(cid)
                        citations_set.add(cid)

            tool_step = ToolStep(
                turn=iterations,
                tool_name=tc.name,
                call_id=tc.id,
                args=tc.args,
                result=result_text,
                is_error=is_error,
            )
            transcript.append(tool_step)

            if audit_hook:
                audit_hook({
                    "kind": "tool",
                    "turn": iterations,
                    "tool": tc.name,
                    "call_id": tc.id,
                    "args_keys": sorted(tc.args.keys()),  # redacted: keys only
                    "status": result_status,
                })

            results.append(
                ToolResult(id=tc.id, name=tc.name, content=result_text, is_error=is_error)
            )

        if guardrail_hit:
            break

        # Commit this round to the conversation and advance the counter.
        messages.append(AssistantMessage(turn.text, turn.tool_calls))
        messages.append(ToolResultMessage(results))
        iterations += 1

    return AgentResult(
        final_text=final_text,
        transcript=transcript,
        tools_used=sorted(tools_used),
        citations=citations,
        writes=writes,
        stop_reason=stop_reason,
        usage=total_usage,
        iterations=iterations,
    )
