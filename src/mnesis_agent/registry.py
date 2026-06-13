"""ToolRegistry — aggregates one or more ToolSources into a single tool list.

Usage
-----
    reg = ToolRegistry()
    reg.add_source(MCPToolSource(url, token))   # or FakeToolSource() for tests
    tools = await reg.list_tools()              # builds/refreshes the index
    result = await reg.dispatch("mnesis_query", {"query": "redis"})

Multiple sources are supported (e.g. Mnesis MCP + a local tool source).  When
two sources expose a tool with the same name the last-added source wins — tools
are registered in add_source order and later registrations overwrite earlier
ones, which matches typical overlay semantics (local tools shadow remote ones).
"""
from __future__ import annotations

from .mcp_client import ToolSource, ToolSpec


class ToolNotFoundError(KeyError):
    """Raised by dispatch() when no registered source owns the requested tool."""


class ToolRegistry:
    """Aggregates ToolSources and routes dispatch() calls to the owning source."""

    def __init__(self) -> None:
        self._sources: list[ToolSource] = []
        self._index: dict[str, ToolSource] = {}  # tool name -> owning source
        self._specs: dict[str, ToolSpec] = {}    # tool name -> spec

    def add_source(self, source: ToolSource) -> None:
        """Register a ToolSource.  Call refresh() (or list_tools()) to index it."""
        self._sources.append(source)

    async def refresh(self) -> list[ToolSpec]:
        """Re-query all sources and rebuild the name→source index.

        Returns the full flat list of ToolSpecs across all sources.
        """
        self._index.clear()
        self._specs.clear()
        all_tools: list[ToolSpec] = []
        for source in self._sources:
            for spec in await source.list_tools():
                self._index[spec.name] = source
                self._specs[spec.name] = spec
                all_tools.append(spec)
        return all_tools

    async def list_tools(self) -> list[ToolSpec]:
        """Return all tools (refreshes the index as a side effect)."""
        return await self.refresh()

    async def dispatch(self, name: str, args: dict) -> str:
        """Route a tool call to the owning source.

        Auto-refreshes the index on the first call (when the index is empty).
        Raises ToolNotFoundError when no source owns the tool.
        """
        if not self._index:
            await self.refresh()
        source = self._index.get(name)
        if source is None:
            raise ToolNotFoundError(
                f"No tool named {name!r} in any registered source. "
                f"Available: {sorted(self._index)}"
            )
        return await source.call_tool(name, args)

    def spec(self, name: str) -> ToolSpec | None:
        """Return the ToolSpec for a tool by name (None if not indexed yet)."""
        return self._specs.get(name)
