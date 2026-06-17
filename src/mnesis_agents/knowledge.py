"""Mnesis as agent memory — its MCP tools, surfaced as LangChain tools.

The package reaches Mnesis ONLY through these MCP tools; it never imports the
``mnesis`` package. ``MCPToolSource`` loads the ``mnesis_*`` tools from the MCP
HTTP endpoint via langchain-mcp-adapters; ``FakeMnesisTools`` is a deterministic
offline stand-in; ``ToolRegistry`` aggregates one or more sources into a single,
collision-free tool list for agents.

Imports of langchain / the adapter are lazy, so this module loads in a minimal
environment and the fake source works with langchain-core alone.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections import Counter
from typing import TYPE_CHECKING, Any

from . import config

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

#: Tool names Mnesis exposes that agents care about (for reference / fake source).
MNESIS_TOOL_NAMES: tuple[str, ...] = (
    "mnesis_query",
    "mnesis_get",
    "mnesis_ingest",
    "mnesis_file_back",
    "mnesis_impact",
)

#: Maintenance/curation tool names Mnesis exposes (the dream-cycle surface). Kept
#: separate from MNESIS_TOOL_NAMES so the everyday read/write fake stays minimal.
MAINTENANCE_TOOL_NAMES: tuple[str, ...] = (
    "mnesis_decay",
    "mnesis_graph_lint",
    "mnesis_review",
    "mnesis_resolve",
    "mnesis_find_duplicates",
    "mnesis_health_report",
)

#: Separator used when the registry namespaces a colliding tool name.
NAMESPACE_SEP = "__"


class MnesisConnectionError(RuntimeError):
    """The Mnesis MCP endpoint could not be reached / authenticated."""


# ── Connection config ─────────────────────────────────────────────────────


def mnesis_connection() -> dict[str, Any]:
    """A langchain-mcp-adapters streamable-HTTP connection for Mnesis, from config.

    Sends the bearer token as an ``Authorization`` header (when set) — the same
    auth the server's ``MNESIS_MCP_TOKEN`` expects.
    """
    conn: dict[str, Any] = {"transport": "streamable_http", "url": config.MNESIS_MCP_URL}
    if config.MNESIS_MCP_TOKEN:
        conn["headers"] = {"Authorization": f"Bearer {config.MNESIS_MCP_TOKEN}"}
    return conn


# ── Tool sources ────────────────────────────────────────────────────────────


class ToolSource(ABC):
    """A named provider of LangChain tools. ``namespace`` disambiguates names
    when the registry aggregates several sources."""

    namespace: str = "tools"

    @abstractmethod
    async def load_tools(self) -> list["BaseTool"]:
        """Return this source's LangChain tools (may hit the network)."""


class MCPToolSource(ToolSource):
    """Loads tools from one or more MCP servers via ``MultiServerMCPClient``.

    ``connections`` maps a server name to a langchain-mcp-adapters connection
    dict, so additional MCP tool servers can be added alongside Mnesis later.
    """

    def __init__(self, connections: dict[str, dict[str, Any]], *, namespace: str = "mnesis") -> None:
        self.namespace = namespace
        self._connections = connections

    async def load_tools(self) -> list["BaseTool"]:
        try:
            from langchain_mcp_adapters.client import MultiServerMCPClient
        except ImportError as exc:  # pragma: no cover - adapter is in the agents core
            raise MnesisConnectionError(
                "langchain-mcp-adapters is not installed; install the agents extra "
                '(`pip install -e ".[agents]"`).'
            ) from exc

        client = MultiServerMCPClient(self._connections)
        try:
            return await client.get_tools()
        except Exception as exc:  # connection refused, 401, DNS, protocol error…
            servers = ", ".join(f"{n}={c.get('url', '?')}" for n, c in self._connections.items())
            raise MnesisConnectionError(
                f"Could not load MCP tools from [{servers}]: {exc}. "
                "Check the endpoint is running, the URL is reachable, and the "
                "bearer token (MNESIS_MCP_TOKEN) matches the server."
            ) from exc


def mnesis_mcp_source() -> MCPToolSource:
    """The default Mnesis MCP tool source, built from config."""
    return MCPToolSource({"mnesis": mnesis_connection()}, namespace="mnesis")


# ── Offline fake source ───────────────────────────────────────────────────


def _build_fake_mnesis_tools() -> list["BaseTool"]:
    """Deterministic stand-ins for the ``mnesis_*`` tools — same names/shapes,
    canned results, no network. Mirrors the real tools' signatures."""
    import json

    from langchain_core.tools import tool

    @tool
    def mnesis_query(query: str, limit: int = 10) -> str:
        """Search the Mnesis knowledge base (BM25 + confidence, graph-augmented). Returns ranked hits as JSON."""
        return json.dumps({
            "query": query,
            "hits": [{
                "id": "atlas", "title": "Project Atlas uses Redis for caching",
                "snippet": "Project Atlas uses Redis as its primary caching layer.",
                "confidence": 0.85, "status": "active",
            }][:limit],
        })

    @tool
    def mnesis_get(page_id: str) -> str:
        """Fetch a knowledge-base page by its id. Returns the page as JSON."""
        return json.dumps({
            "id": page_id, "title": "Project Atlas uses Redis for caching",
            "body": "Project Atlas uses Redis as its primary caching layer.",
            "confidence": 0.85, "status": "active",
            "tags": ["project:atlas", "library:redis"],
        })

    @tool
    def mnesis_ingest(text: str, source_ref: str) -> str:
        """Ingest a source into Mnesis (filtered, extracted, routed). Returns the outcome."""
        return (
            f"ingested page: stub-{source_ref}\n"
            "title: Stub page\naction: new\nredactions: 0"
        )

    @tool
    def mnesis_file_back(question: str, answer: str, quality_score: float | None = None) -> str:
        """File a synthesized answer back as a durable digest page (the compounding step)."""
        return json.dumps({"filed": True, "digest_id": "stub-digest", "question": question})

    @tool
    def mnesis_impact(entity: str, depth: int = 3) -> str:
        """What depends on / uses an entity — reverse graph traversal with paths."""
        return json.dumps({
            "entity": entity,
            "affected": [{
                "ref": "decision:auth-migration", "hop": 1, "predicate": "depends_on",
                "path": ["decision:auth-migration", entity],
            }],
        })

    return [mnesis_query, mnesis_get, mnesis_ingest, mnesis_file_back, mnesis_impact]


class FakeMnesisTools(ToolSource):
    """Offline source exposing deterministic ``mnesis_*`` LangChain tools, so the
    whole agent layer is testable without a running Mnesis."""

    namespace = "mnesis"

    async def load_tools(self) -> list["BaseTool"]:
        return _build_fake_mnesis_tools()


def _build_fake_maintenance_tools() -> list["BaseTool"]:
    """Deterministic stand-ins for the Mnesis **maintenance** tools (the
    dream-cycle surface) — canned JSON results, no network. They mirror the real
    tools' names/signatures; the review fake bundles the per-page ``source_count``
    /``last_confirmed`` that the real flow gathers via ``mnesis_get``, so the
    triage skill is exercisable offline from a single call."""
    import json

    from langchain_core.tools import tool

    @tool
    def mnesis_decay() -> str:
        """Run the decay/lifecycle pass (age knowledge, active<->stale). Returns transition counts as JSON."""
        return json.dumps({"scanned": 6, "restaled": 1, "reactivated": 0, "unchanged": 5})

    @tool
    def mnesis_graph_lint(fix: bool = False) -> str:
        """Lint the knowledge graph; with fix=True apply the safe auto-fixes. Returns a structured report as JSON."""
        if fix:
            return json.dumps({
                "fixed": True,
                "fixed_categories": {"duplicate_edges": 1, "stale_only_edges": 0, "confidence_updates": 1},
                "flagged_items": [
                    {"category": "undeclared_entities", "ref": "library:redis",
                     "detail": "used by page atlas-redis but not declared as a tag"}
                ],
            })
        return json.dumps({
            "fixed": False,
            "fixable_categories": {"duplicate_edges": 1, "stale_only_edges": 0, "confidence_updates": 1},
            "flagged_items": [
                {"category": "undeclared_entities", "ref": "library:redis",
                 "detail": "used by page atlas-redis but not declared as a tag"}
            ],
        })

    @tool
    def mnesis_review() -> str:
        """List open contradiction reviews (with per-page confidence/source_count/last_confirmed) as JSON."""
        return json.dumps({"open": [
            {"review_id": 1, "detail": "Conflicting cache backend for Atlas",
             "pages": [
                 {"id": "atlas-redis", "title": "Atlas uses Redis", "confidence": 0.82,
                  "source_count": 3, "last_confirmed": "2026-06-10T12:00:00Z"},
                 {"id": "atlas-memcached", "title": "Atlas uses Memcached", "confidence": 0.45,
                  "source_count": 1, "last_confirmed": "2026-02-01T12:00:00Z"},
             ]},
        ]})

    @tool
    def mnesis_resolve(review_id: int, keep_id: str) -> str:
        """Resolve a contradiction (WRITE): keep keep_id, supersede the other. Returns the outcome as JSON."""
        return json.dumps({"resolved": review_id, "kept": keep_id})

    @tool
    def mnesis_find_duplicates(limit: int = 20) -> str:
        """Heuristic near-duplicate candidate pairs as JSON (read-only; proposes nothing)."""
        return json.dumps({"candidates": [
            {"page_a": "atlas-redis", "page_b": "atlas-redis-cache",
             "title_a": "Project Atlas uses Redis for caching", "title_b": "Atlas uses Redis as its cache",
             "similarity": 0.62, "signals": {"title": 0.5, "tags": 1.0, "edges": 1.0, "fts": True},
             "rationale": "shared tags 1.00 (project:atlas, library:redis); shared edges 1.00; co-retrieved by FTS"},
            {"page_a": "pg-backups", "page_b": "pg-restore",
             "title_a": "Postgres backups run nightly", "title_b": "Postgres restore procedure",
             "similarity": 0.28, "signals": {"title": 0.33, "tags": 0.5, "edges": 0.0, "fts": True},
             "rationale": "title overlap 0.33; shared tags 0.50 (library:postgres); co-retrieved by FTS"},
        ]})

    @tool
    def mnesis_health_report() -> str:
        """Read-only system health snapshot as JSON (counts, gaps, cache freshness)."""
        return json.dumps({
            "pages_total": 7,
            "by_status": {"active": 6, "stale": 1},
            "by_kind": {"fact": 5, "digest": 1, "note": 1},
            "no_sources": ["orphan-note"],
            "low_confidence": 2,
            "low_confidence_pages": ["old-fact", "transient-bug"],
            "stale": 1,
            "open_contradictions": 1,
            "graph": {"entities": 12, "edges": 9, "demoted": 1},
            "orphan_entities": 1,
            "undeclared_entities": 1,
            "dangling_structural": 0,
            "index": {"markdown_pages": 7, "indexed_pages": 7, "fresh": True,
                      "missing_from_index": [], "extra_in_index": []},
            "graph_index": {"present": True, "fresh": True,
                            "missing_page_nodes": [], "extra_page_nodes": []},
        })

    return [mnesis_decay, mnesis_graph_lint, mnesis_review, mnesis_resolve,
            mnesis_find_duplicates, mnesis_health_report]


class FakeMaintenanceTools(ToolSource):
    """Offline deterministic stand-ins for the Mnesis maintenance tools, so the
    dream-cycle maintenance skills are exercisable without a running Mnesis."""

    namespace = "mnesis"

    async def load_tools(self) -> list["BaseTool"]:
        return _build_fake_maintenance_tools()


# ── Registry ──────────────────────────────────────────────────────────────


class ToolRegistry:
    """Aggregates tool sources into one list for agents.

    Names are namespaced *only when they collide* across sources (prefixed with
    the owning source's ``namespace`` + ``__``), so the common single-source case
    keeps clean tool names. Pass ``force_namespace=True`` to always prefix.
    """

    def __init__(self, sources: list[ToolSource] | None = None) -> None:
        self._sources: list[ToolSource] = list(sources or [])

    def add_source(self, source: ToolSource) -> None:
        self._sources.append(source)

    async def get_tools(self, *, force_namespace: bool = False) -> list["BaseTool"]:
        loaded: list[tuple[ToolSource, BaseTool]] = []
        for source in self._sources:
            for t in await source.load_tools():
                loaded.append((source, t))

        counts = Counter(t.name for _, t in loaded)
        out: list[BaseTool] = []
        for source, t in loaded:
            if force_namespace or counts[t.name] > 1:
                # Rename a *copy* (preserves dispatch — MCP tools call by their
                # captured original name, and the original object is untouched).
                t = t.model_copy(update={"name": f"{source.namespace}{NAMESPACE_SEP}{t.name}"})
            out.append(t)
        return out
