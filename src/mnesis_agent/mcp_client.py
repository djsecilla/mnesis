"""MCP HTTP client and ToolSource abstraction.

Public surface
--------------
ToolSpec          — normalized tool descriptor (name, description, input_schema).
ToolSource        — ABC: list_tools() / call_tool().
MCPToolSource     — real MCP HTTP connection via the streamable-HTTP transport.
MCPConnectionError / MCPAuthError / MCPToolError — typed error hierarchy.

MCPToolSource opens a fresh session per call (simple, testable).  A long-lived
session pool is a straightforward upgrade for Phase B.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


# ── Types ────────────────────────────────────────────────────────────────────


@dataclass
class ToolSpec:
    """Normalized tool descriptor — the common currency between sources and the registry."""

    name: str
    description: str
    input_schema: dict = field(default_factory=dict)


# ── Errors ───────────────────────────────────────────────────────────────────


class MCPConnectionError(RuntimeError):
    """The MCP endpoint is unreachable or returned an unexpected HTTP error."""


class MCPAuthError(RuntimeError):
    """The MCP endpoint rejected the bearer token (HTTP 401)."""


class MCPToolError(RuntimeError):
    """A tool call returned isError=True from the server."""


# ── Abstract source ──────────────────────────────────────────────────────────


class ToolSource(ABC):
    """Async interface implemented by both the real MCP client and the fake."""

    @abstractmethod
    async def list_tools(self) -> list[ToolSpec]: ...

    @abstractmethod
    async def call_tool(self, name: str, args: dict) -> str: ...


# ── Helpers ──────────────────────────────────────────────────────────────────


def _unwrap(exc: BaseException) -> BaseException:
    """Unwrap the first leaf exception from an ExceptionGroup (Python 3.11+)."""
    if isinstance(exc, ExceptionGroup) and exc.exceptions:
        return _unwrap(exc.exceptions[0])
    return exc


def _raise_transport_error(url: str, exc: Exception) -> None:
    """Always raises a typed error; never returns."""
    inner = _unwrap(exc)
    if isinstance(inner, httpx.ConnectError):
        raise MCPConnectionError(
            f"Cannot reach MCP endpoint {url!r}: {inner}"
        ) from inner
    if isinstance(inner, httpx.HTTPStatusError):
        code = inner.response.status_code
        if code == 401:
            raise MCPAuthError(
                f"MCP endpoint rejected the bearer token (401): {url!r}"
            ) from inner
        raise MCPConnectionError(
            f"MCP endpoint returned HTTP {code}: {url!r}"
        ) from inner
    raise MCPConnectionError(f"MCP transport error at {url!r}: {inner}") from inner


# ── Real MCP HTTP source ─────────────────────────────────────────────────────


class MCPToolSource(ToolSource):
    """Connects to a running Mnesis MCP HTTP endpoint.

    Opens a fresh session per call (simple, no leaked resources).  Auth is
    injected as an ``Authorization: Bearer`` header on the underlying httpx
    client — identical to what nginx injects for the browser.
    """

    def __init__(self, url: str, token: str = "") -> None:
        self._url = url
        self._headers: dict[str, str] = (
            {"Authorization": f"Bearer {token}"} if token else {}
        )

    async def list_tools(self) -> list[ToolSpec]:
        try:
            async with httpx.AsyncClient(
                headers=self._headers,
                timeout=httpx.Timeout(30, read=120),
            ) as http:
                async with streamable_http_client(self._url, http_client=http) as (r, w, _):
                    async with ClientSession(r, w) as session:
                        await session.initialize()
                        result = await session.list_tools()
            return [
                ToolSpec(
                    name=t.name,
                    description=t.description or "",
                    input_schema=dict(t.inputSchema) if t.inputSchema else {},
                )
                for t in result.tools
            ]
        except (MCPConnectionError, MCPAuthError, MCPToolError):
            raise
        except Exception as exc:
            _raise_transport_error(self._url, exc)
            raise  # unreachable; satisfies type checkers

    async def call_tool(self, name: str, args: dict) -> str:
        try:
            async with httpx.AsyncClient(
                headers=self._headers,
                timeout=httpx.Timeout(30, read=120),
            ) as http:
                async with streamable_http_client(self._url, http_client=http) as (r, w, _):
                    async with ClientSession(r, w) as session:
                        await session.initialize()
                        result = await session.call_tool(name, args)
            if result.isError:
                msgs = [c.text for c in result.content if hasattr(c, "text")]
                raise MCPToolError(
                    f"Tool {name!r} returned an error: {' '.join(msgs)}"
                )
            parts = [c.text for c in result.content if hasattr(c, "text")]
            return "\n".join(parts)
        except (MCPConnectionError, MCPAuthError, MCPToolError):
            raise
        except Exception as exc:
            _raise_transport_error(self._url, exc)
            raise  # unreachable
