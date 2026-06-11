"""FastMCP server exposing the mnesis wiki tools.

This is the agent-facing surface: Claude Code (and any MCP client) can ingest
sources, query the index, fetch pages, and file synthesized answers back. The
tools are thin orchestration over the core modules (filters, ingest, store,
search) — no business logic lives here that isn't in those modules.

Newly written pages are ``search.upsert``-ed into the index immediately, so a
``mnesis_file_back`` answer (or a fresh ingest) surfaces on the next
``mnesis_query`` — the compounding loop the PoC exists to demonstrate.

Two transports (selected by ``MNESIS_MCP_TRANSPORT``): **stdio** (default; local
Claude Code spawns it as a subprocess — unchanged) and **http** (streamable
HTTP for container deployment, with a ``GET /health`` endpoint and optional
bearer-token auth). Verified against mcp 1.27.x: ``FastMCP``, ``@mcp.tool()``,
``mcp.custom_route``, ``mcp.streamable_http_app()``, ``mcp.run()``.
"""

from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse

from . import config, confidence, graph, graph_lint, ingest, lifecycle, search, state, store
from .filters import scrub
from .store import Page

log = logging.getLogger(__name__)

mcp = FastMCP("mnesis")


def _open_contradiction_ids() -> set[str]:
    """Page ids that appear in an open contradiction review."""
    ids: set[str] = set()
    for r in state.list_open_reviews():
        ids.add(r["page_a"])
        ids.add(r["page_b"])
    return ids


def _page_confidence(page: Page) -> float:
    return confidence.compute_confidence(page, access=state.get_access(page.id))[0]


def _related_entities(page_id: str, page: Page | None = None) -> list[str]:
    """Graph entity refs a page declares (best-effort; never raises on a query)."""
    try:
        return graph.related_entities(page or store.read_page(page_id))
    except Exception:
        return []


def _heuristic_quality(answer: str) -> float:
    """Cheap stand-in quality score when the caller supplies none (CLAUDE.md §9).

    A longer, more developed answer scores higher; capped at 1.0. Deliberately
    simple — LLM-as-judge scoring is Phase 5.
    """
    words = len(answer.split())
    return round(min(1.0, words / 25.0), 2)


def _digest_body(answer: str, sources: list[str]) -> str:
    body = answer.strip()
    if sources:
        body += "\n\nSynthesized from: " + ", ".join(sources) + "."
    return body


@mcp.tool()
def mnesis_ingest(text: str, source_ref: str) -> str:
    """Filter, extract, and write a source as a canonical fact page.

    Returns the created page's id, title, tags, and how many secrets/PII were
    redacted at the boundary.
    """
    _, findings = scrub(text)  # for the redaction count in the summary
    page = ingest.ingest_source(text, source_ref)
    search.upsert(page)
    tags = ", ".join(page.tags) if page.tags else "(none)"
    return (
        f"ingested page: {page.id}\n"
        f"title: {page.title}\n"
        f"tags: {tags}\n"
        f"redactions: {len(findings)}"
    )


@mcp.tool()
def mnesis_query(query: str, limit: int = 10, include_stale: bool = False) -> str:
    """Search the wiki: BM25 blended with confidence, augmented by the graph.

    Results are ordered by a blended score (keyword match × confidence + a small
    graph-proximity boost). When the query resolves to an entity, pages reachable
    through the knowledge graph are folded in even if they lack the keyword, each
    marked ``↳ graph`` and grounded by the connecting edge. Stale pages are
    excluded unless ``include_stale=True``. Reading top hits records access.
    """
    hits = graph.graph_query(query, limit, include_stale=include_stale)
    if not hits:
        return f'no results for "{query}"'
    contradicted = _open_contradiction_ids()
    lines = []
    for i, h in enumerate(hits, 1):
        mark = "" if h.status == "active" else f" [{h.status}]"
        if h.id in contradicted:
            mark += " ⚠ contradiction under review"
        if h.grounding is not None:
            mark += " ↳ graph"
        lines.append(
            f"{i}. {h.id} — {h.title}{mark} "
            f"(conf {h.confidence:.2f}, graph {h.graph_proximity:.2f}, score {h.final_score:.3f})"
        )
        lines.append(f"   {h.snippet}")
        related = _related_entities(h.id)
        if related:
            lines.append(f"   related entities: {', '.join(related)}")
    out = "\n".join(lines)
    # Reinforcement: record access for the surfaced top hits (cheap, never fails).
    for h in hits[:search._ACCESS_TOP_N]:
        search.record_and_reindex(h.id)
    return out


@mcp.tool()
def mnesis_get(page_id: str) -> str:
    """Return a page's full Markdown (frontmatter + body), prefixed with its
    current derived confidence and status.

    Confidence is derived (not stored in Markdown), so it is shown in a header
    line, not the frontmatter. Reading a page records an access (reinforcement)
    and refreshes its cached confidence.
    """
    if "/" in page_id or "\\" in page_id:
        return f"invalid page id: {page_id}"
    path = config.PAGES_DIR / f"{page_id}.md"
    if not path.exists():
        return f"no such page: {page_id}"
    page = store.read_page(page_id)
    header = f"[{page_id}] status: {page.status} | confidence: {_page_confidence(page):.2f}"
    if page_id in _open_contradiction_ids():
        header += " | ⚠ contradiction under review (see `mnesis_review`)"
    related = _related_entities(page_id, page)
    if related:
        header += f"\nrelated entities: {', '.join(related)}"
    md = path.read_text(encoding="utf-8")
    search.record_and_reindex(page_id)  # reinforcement on read
    return f"{header}\n\n{md}"


@mcp.tool()
def mnesis_file_back(question: str, answer: str, quality_score: float | None = None) -> str:
    """File a synthesized answer back as a durable ``digest`` page (compounding).

    If ``quality_score`` (or the internal heuristic when ``None``) is at least
    ``MNESIS_FILEBACK_THRESHOLD``, write a ``kind=digest`` page linking the
    question and answer and return its id. Otherwise file nothing and return the
    reason. Digest pages are tagged ``kind:digest`` so they never masquerade as
    primary sourced facts (CLAUDE.md §5, §9).
    """
    score = quality_score if quality_score is not None else _heuristic_quality(answer)
    threshold = config.MNESIS_FILEBACK_THRESHOLD
    if score < threshold:
        return f"below threshold, not filed (score {score:.2f} < {threshold:.2f})"

    # Link the facts the answer drew on: top keyword hits for the question.
    sources = [h.id for h in search.search(question, limit=3)]
    page = Page(
        id=store.make_id(question),
        title=question,
        body=_digest_body(answer, sources),
        sources=sources,
        source_count=max(1, len(sources)),
        tags=["kind:digest"],
        kind="digest",
        question=question,
    )
    store.write_page(page)
    search.upsert(page)
    return f"filed digest: {page.id} (score {score:.2f})"


@mcp.tool()
def mnesis_list() -> str:
    """List every page: id, kind/status, and title."""
    pages = store.list_pages()
    if not pages:
        return "(no pages)"
    return "\n".join(f"{p.id} [{p.kind}/{p.status}] — {p.title}" for p in pages)


@mcp.tool()
def mnesis_rebuild() -> str:
    """Rebuild the rebuildable caches from Markdown: the search index AND the
    knowledge graph. The durable state store is never cleared."""
    n = search.rebuild()
    g = graph.rebuild_graph()
    return (
        f"rebuilt search index from {n} page(s); "
        f"graph: {g['entities']} entities, {g['edges']} edges ({g['demoted']} demoted)"
    )


@mcp.tool()
def mnesis_impact(entity: str, depth: int = 3) -> str:
    """What would be affected by changing ``entity`` (a ``type:value`` ref).

    Reverse-traverses ``depends_on``/``uses`` edges: returns the affected entities
    with their dependency path back to ``entity``, the connecting predicate, edge
    confidence, and the grounding pages. Demoted (stale-only) edges excluded.
    """
    affected = graph.impact(entity, depth=depth)
    if not affected:
        return f"nothing depends on or uses {entity}"
    lines = [f"impact of changing {entity}:"]
    for a in affected:
        path = " -> ".join(a["path"])
        pages = ", ".join(a["grounding_pages"])
        lines.append(
            f"  {a['ref']} (hop {a['hop']}, {a['predicate']}, conf {a['confidence']:.2f})"
        )
        lines.append(f"    path: {path}   [grounded by: {pages}]")
    return "\n".join(lines)


@mcp.tool()
def mnesis_entity(ref: str) -> str:
    """Inspect a graph entity (a ``type:value`` ref): its type, the pages that
    declare/assert it, and its typed edges with confidence and grounding pages.
    Traversal/edges are confidence-weighted and exclude stale (demoted) edges by
    default."""
    ent = graph.entity(ref)
    if ent is None:
        return f"no such entity: {ref}"
    lines = [f"{ref} (type: {ent['type']})"]
    if ent["pages"]:
        lines.append(f"grounded in pages: {', '.join(ent['pages'])}")
    if not ent["edges"]:
        lines.append("edges: (none)")
    else:
        lines.append("edges:")
        for e in ent["edges"]:
            tag = " [demoted]" if e["demoted"] else ""
            lines.append(
                f"  {e['s']} -{e['p']}-> {e['o']} "
                f"(conf {e['confidence']:.2f}, {e['assertion_count']} src{tag}) "
                f"[pages: {', '.join(e['source_pages'])}]"
            )
    return "\n".join(lines)


@mcp.tool()
def mnesis_neighbors(ref: str, predicate: str | None = None, direction: str = "out") -> str:
    """Adjacent entities of ``ref`` via non-demoted edges. ``direction`` is
    ``out``/``in``/``both``; optional ``predicate`` filter. Each result cites the
    pages behind its edge."""
    try:
        ns = graph.neighbors(ref, predicate=predicate, direction=direction)
    except ValueError as exc:
        return str(exc)
    if not ns:
        return f"no neighbors for {ref}"
    lines = [f"neighbors of {ref} ({direction}{', ' + predicate if predicate else ''}):"]
    for n in ns:
        arrow = "->" if n["direction"] == "out" else "<-"
        lines.append(
            f"  {arrow} {n['ref']} ({n['predicate']}, conf {n['confidence']:.2f}) "
            f"[pages: {', '.join(n['source_pages'])}]"
        )
    return "\n".join(lines)


@mcp.tool()
def mnesis_traverse(ref: str, predicate: str | None = None, depth: int = 2) -> str:
    """Entities reachable from ``ref`` within ``depth`` hops (out edges), with the
    path and predicates. Cycle-safe, confidence-ordered upstream, excludes stale
    (demoted) edges."""
    rows = graph.traverse(ref, predicate=predicate, depth=depth)
    if not rows:
        return f"nothing reachable from {ref}"
    lines = [f"reachable from {ref} (depth {depth}):"]
    for r in rows:
        path = " -> ".join(r["path"])
        lines.append(f"  [{r['depth']}] {path}")
    return "\n".join(lines)


@mcp.tool()
def mnesis_graph_stats() -> str:
    """Knowledge-graph size: node and edge counts by type/predicate, plus the
    demoted-edge count."""
    s = graph.graph_stats()
    lines = [f"entities: {s['entities']}   edges: {s['edges']}   demoted: {s['demoted']}"]
    if s.get("entities_by_type"):
        lines.append("by entity type: " + ", ".join(f"{t}={n}" for t, n in s["entities_by_type"].items()))
    if s.get("edges_by_predicate"):
        lines.append("by predicate: " + ", ".join(f"{p}={n}" for p, n in s["edges_by_predicate"].items()))
    return "\n".join(lines)


@mcp.tool()
def mnesis_graph_lint(fix: bool = False) -> str:
    """Lint the knowledge graph. Report-only by default; with ``fix=True`` applies
    the safe auto-fixes (merge duplicate edges, demote stale-only edges, recompute
    edge confidence) and flags the rest (undeclared/orphan entities, dangling
    structural edges) for human review. Idempotent; never deletes an edge with an
    active supporting page."""
    return graph_lint.graph_lint(fix=fix).summary()


@mcp.tool()
def mnesis_decay() -> str:
    """Run the decay/lifecycle pass: recompute confidence corpus-wide and
    transition pages between active and stale (aged, unread, low-confidence pages
    go stale; reinforced ones revive). Idempotent. Returns the transition counts."""
    s = lifecycle.recompute_all()
    return (
        f"decay: scanned {s['scanned']}, restaled {s['restaled']}, "
        f"reactivated {s['reactivated']}, unchanged {s['unchanged']}"
    )


@mcp.tool()
def mnesis_review() -> str:
    """List open contradiction reviews: queue id, both pages (with current
    confidence and title), and the conflict detail."""
    reviews = state.list_open_reviews()
    if not reviews:
        return "(no open contradictions)"
    lines = []
    for r in reviews:
        parts = []
        for pid in (r["page_a"], r["page_b"]):
            try:
                page = store.read_page(pid)
                parts.append(f"{pid} (conf {_page_confidence(page):.2f}) \"{page.title}\"")
            except FileNotFoundError:
                parts.append(f"{pid} (missing)")
        lines.append(f"#{r['id']}: {parts[0]}  <->  {parts[1]}")
        lines.append(f"   detail: {r['detail']}")
        lines.append(f"   resolve with: mnesis resolve {r['id']} --keep <page_id>")
    return "\n".join(lines)


@mcp.tool()
def mnesis_resolve(review_id: int, keep_id: str) -> str:
    """Resolve an open contradiction by keeping ``keep_id`` and superseding the
    other page (→ stale). Clears the mutual ``contradicts`` link (lifting the
    kept page's confidence) and closes the review. Goes through
    ``store.supersede`` — no ad hoc edits; the loser stays as stale history.
    """
    review = next((r for r in state.list_open_reviews() if r["id"] == review_id), None)
    if review is None:
        return f"no open review with id {review_id}"
    pair = (review["page_a"], review["page_b"])
    if keep_id not in pair:
        return f"keep id {keep_id} is not part of review {review_id} ({pair[0]} / {pair[1]})"
    other_id = pair[1] if keep_id == pair[0] else pair[0]

    kept = store.read_page(keep_id)
    store.supersede(other_id, kept)  # other -> stale, mutual contradicts cleared, one commit
    search.upsert(kept)
    search.upsert(store.read_page(other_id))
    state.resolve_review(review_id)
    return f"resolved review {review_id}: kept {keep_id}, superseded {other_id}"


# --- HTTP transport: health endpoint, bearer auth, server bootstrap --------


def _health_payload() -> dict:
    """Cheap liveness + quick stats — no LLM call."""
    return {
        "status": "ok",
        "pages": len(store.list_pages()),
        "index_present": (config.INDEX_DIR / "wiki.db").exists(),
        "graph_present": (config.INDEX_DIR / "graph.db").exists(),
    }


@mcp.custom_route("/health", methods=["GET"])
async def _health(_request):
    """Unauthenticated health probe (safe for load balancers)."""
    return JSONResponse(_health_payload())


class _BearerAuthMiddleware:
    """ASGI middleware requiring ``Authorization: Bearer <token>`` on every HTTP
    request except ``/health``. Installed only when a token is configured."""

    def __init__(self, app, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") == "http" and scope.get("path") != "/health":
            headers = dict(scope.get("headers") or [])
            presented = headers.get(b"authorization", b"").decode()
            if presented != f"Bearer {self.token}":
                await JSONResponse({"error": "unauthorized"}, status_code=401)(
                    scope, receive, send
                )
                return
        await self.app(scope, receive, send)


def build_http_app():
    """Build the streamable-HTTP ASGI app: MCP at ``/mcp``, ``GET /health``, and
    bearer auth when ``MNESIS_MCP_TOKEN`` is set. Tool functions are reused as-is."""
    app = mcp.streamable_http_app()
    if config.MNESIS_MCP_TOKEN:
        app.add_middleware(_BearerAuthMiddleware, token=config.MNESIS_MCP_TOKEN)
    return app


def serve() -> None:
    """Run the server using the transport selected by ``MNESIS_MCP_TRANSPORT``."""
    if config.MNESIS_MCP_TRANSPORT == "http":
        import uvicorn

        if not config.MNESIS_MCP_TOKEN:
            log.warning(
                "MCP HTTP transport starting WITHOUT a bearer token "
                "(MNESIS_MCP_TOKEN unset). This endpoint can ingest and modify "
                "knowledge — treat it as privileged and restrict network access."
            )
        uvicorn.run(
            build_http_app(), host=config.MNESIS_MCP_HOST, port=config.MNESIS_MCP_PORT
        )
    else:
        mcp.run()  # stdio (default) — unchanged for local Claude Code


if __name__ == "__main__":
    serve()
