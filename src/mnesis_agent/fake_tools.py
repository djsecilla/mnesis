"""FakeToolSource — deterministic in-process ToolSource for offline tests.

Stands in for a running Mnesis instance with no network and no MCP server.
Canned responses mimic the JSON shapes returned by the real mnesis_* tools so
tests can verify agent logic without an infrastructure dependency.

Customisable: pass ``tools`` and/or ``responses`` to override the defaults for
any individual test.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from .mcp_client import MCPToolError, ToolSource, ToolSpec


# ── Default canned tool specs ────────────────────────────────────────────────

_STR = {"type": "string"}

DEFAULT_TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="mnesis_query",
        description="Search the knowledge base for pages matching a query.",
        input_schema={
            "type": "object",
            "properties": {"query": _STR, "limit": {"type": "integer"}},
            "required": ["query"],
        },
    ),
    ToolSpec(
        name="mnesis_get",
        description="Retrieve a knowledge-base page by its id.",
        input_schema={
            "type": "object",
            "properties": {"id": _STR},
            "required": ["id"],
        },
    ),
    ToolSpec(
        name="mnesis_ingest",
        description="Ingest raw text as a new knowledge source.",
        input_schema={
            "type": "object",
            "properties": {
                "text": _STR,
                "source_ref": _STR,
            },
            "required": ["text"],
        },
    ),
    ToolSpec(
        name="mnesis_file_back",
        description="File a synthesised answer back as a digest page.",
        input_schema={
            "type": "object",
            "properties": {"question": _STR, "answer": _STR, "quality_score": {"type": "number"}},
            "required": ["question", "answer"],
        },
    ),
    ToolSpec(
        name="mnesis_list",
        description="List pages in the knowledge base.",
        input_schema={
            "type": "object",
            "properties": {"limit": {"type": "integer"}, "status": _STR},
        },
    ),
]

# ── Default canned responses (JSON strings) ──────────────────────────────────

DEFAULT_RESPONSES: dict[str, str] = {
    "mnesis_query": json.dumps({
        "hits": [
            {
                "id": "atlas",
                "title": "Project Atlas uses Redis for caching",
                "snippet": "Project Atlas uses Redis as its primary caching layer.",
                "bm25_score": 1.2,
                "confidence": 0.85,
                "graph_proximity": 0.0,
                "final_score": 0.925,
                "status": "active",
            }
        ]
    }),
    "mnesis_get": json.dumps({
        "id": "atlas",
        "title": "Project Atlas uses Redis for caching",
        "body": "Project Atlas uses Redis as its primary caching layer.",
        "confidence": 0.85,
        "status": "active",
        "tags": ["project:atlas", "library:redis"],
    }),
    "mnesis_ingest": json.dumps({
        "action_taken": "new",
        "page_id": "stub-page-abc123",
        "superseded_id": None,
        "review_id": None,
        "redaction_count": 0,
    }),
    "mnesis_file_back": json.dumps({
        "filed": True,
        "digest_id": "stub-digest-abc123",
        "message": "Filed as digest page.",
    }),
    "mnesis_list": json.dumps({
        "pages": [
            {
                "id": "atlas",
                "title": "Project Atlas uses Redis for caching",
                "kind": "fact",
                "status": "active",
                "confidence": 0.85,
                "updated": "2026-06-10T10:00:00Z",
                "tags": ["project:atlas", "library:redis"],
            }
        ],
        "total": 1,
    }),
}


# ── FakeToolSource ────────────────────────────────────────────────────────────


class FakeToolSource(ToolSource):
    """In-process ToolSource with deterministic canned responses.

    Used in place of MCPToolSource when no network or running Mnesis is needed.
    Customise per-test by passing ``tools`` and/or ``responses`` overrides.
    """

    def __init__(
        self,
        tools: list[ToolSpec] | None = None,
        responses: dict[str, str] | None = None,
    ) -> None:
        self._tools = list(tools) if tools is not None else list(DEFAULT_TOOLS)
        self._responses: dict[str, str] = {**DEFAULT_RESPONSES, **(responses or {})}

    async def list_tools(self) -> list[ToolSpec]:
        return list(self._tools)

    async def call_tool(self, name: str, args: dict) -> str:
        if name not in self._responses:
            raise MCPToolError(f"Fake: unknown tool {name!r}")
        return self._responses[name]
