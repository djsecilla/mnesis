"""Optional in-process local tools — the seam for non-Mnesis capabilities.

A ``LocalToolSource`` is a ``ToolSource`` (same interface as the MCP client) that
runs tools *in this process* — e.g. a web search/fetch tool. It can be added to
the registry alongside the Mnesis MCP source.

**Off by default.** A plain run starts with only the Mnesis tools. The example
``web_search`` tool is registered only when ``MNESIS_AGENT_ENABLE_LOCAL_TOOLS``
is set. Even when registered, the policy layer (A6) only permits a profile to
call a local tool if the profile *allows local tools* and the tool name is in
its (extended) allowlist — and that is restricted to the **research** profile
(``Archetype.allow_local_tools``). The assistant and ingest-daemon can never
call local tools, registered or not.
"""
from __future__ import annotations

from typing import Awaitable, Callable

from . import config
from .mcp_client import MCPToolError, ToolSource, ToolSpec

#: A local tool implementation: async (args) -> result string.
LocalToolFn = Callable[[dict], Awaitable[str]]


class LocalToolSource(ToolSource):
    """In-process tool source. Register tools, then use like any ToolSource."""

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._fns: dict[str, LocalToolFn] = {}

    def register(self, spec: ToolSpec, fn: LocalToolFn) -> None:
        self._specs[spec.name] = spec
        self._fns[spec.name] = fn

    def tool_names(self) -> frozenset[str]:
        return frozenset(self._specs)

    async def list_tools(self) -> list[ToolSpec]:
        return list(self._specs.values())

    async def call_tool(self, name: str, args: dict) -> str:
        fn = self._fns.get(name)
        if fn is None:
            raise MCPToolError(f"Local tool {name!r} is not registered")
        return await fn(args)


# ── Example local tool: web_search ────────────────────────────────────────────

WEB_SEARCH_SPEC = ToolSpec(
    name="web_search",
    description=(
        "Search the public web for a query and return a short list of results. "
        "Use only to supplement the knowledge base, never to replace it."
    ),
    input_schema={
        "type": "object",
        "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
        "required": ["query"],
    },
)


async def _web_search_demo(args: dict) -> str:
    """Deterministic, offline demo implementation of web_search.

    Ships as an *example* only: it performs no network I/O and returns a fixed
    structured note. A real deployment would replace this fn with an httpx call
    to a search backend; the seam (spec + registration) stays the same.
    """
    import json

    query = str(args.get("query", "")).strip()
    return json.dumps({
        "query": query,
        "results": [],
        "note": "web_search demo tool: no external search backend configured.",
    })


def make_example_local_source() -> LocalToolSource:
    """A LocalToolSource with the example web_search tool registered."""
    src = LocalToolSource()
    src.register(WEB_SEARCH_SPEC, _web_search_demo)
    return src


def build_local_tool_source() -> LocalToolSource | None:
    """Return the configured local tool source, or None when disabled.

    Disabled unless ``MNESIS_AGENT_ENABLE_LOCAL_TOOLS`` is set — so a plain run
    starts with only the Mnesis tools.
    """
    if not config.MNESIS_AGENT_ENABLE_LOCAL_TOOLS:
        return None
    return make_example_local_source()
