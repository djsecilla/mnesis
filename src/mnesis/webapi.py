"""Browser-friendly REST + SSE gateway for the web UI.

Mounted under ``/api`` in the **same** Starlette app the MCP server serves (see
``mcp_server.build_http_app``), so it reuses one process, one port, and the same
bearer-token auth (``MNESIS_MCP_TOKEN`` guards ``/api/*``; ``/health`` stays open).

These routes are **thin adapters** — no business logic lives here that isn't in
``store`` / ``search`` / ``graph`` / ``confidence`` / ``mcp_server``. All graph
access goes through the ``GraphBackend`` interface (via ``graph.*``).

Chat is grounded: it answers ONLY from retrieved wiki pages, cites them inline as
``[[page-id]]``, and says so (with zero citations) when the wiki has nothing.
"""

from __future__ import annotations

import json
import re

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from sse_starlette.sse import EventSourceResponse

from . import config, confidence, graph, llm, search, state, store

# How many retrieved pages ground a chat answer.
CHAT_TOP_N = 5
# Bounds on the /api/graph payload.
_MAX_NODES = 60
_MAX_OVERVIEW_NODES = 40

#: Grounded-answer contract. Citation convention: cite each page a claim draws on
#: inline as ``[[page-id]]`` (the id shown before each page block).
GROUNDED_SYSTEM_PROMPT = """You answer questions using ONLY the wiki pages provided \
below — never from outside knowledge or memory.

Rules:
- Use only facts stated in the provided pages.
- Cite the page(s) each claim draws on inline, as [[page-id]] (the id shown before
  each page block).
- If the provided pages do not contain the answer, say so plainly and cite nothing.

Be concise and grounded."""

_CITE_RE = re.compile(r"\[\[([^\]]+)\]\]")


# --- shaping helpers (pure) -------------------------------------------------


def _conf(page: store.Page) -> float:
    return confidence.compute_confidence(page, access=state.get_access(page.id))[0]


def _open_contradiction_ids() -> set[str]:
    ids: set[str] = set()
    for r in state.list_open_reviews():
        ids.add(r["page_a"])
        ids.add(r["page_b"])
    return ids


def _page_summary(page: store.Page) -> dict:
    return {
        "id": page.id,
        "title": page.title,
        "kind": page.kind,
        "status": page.status,
        "confidence": round(_conf(page), 4),
        "updated": page.updated,
        "tags": page.tags,
    }


def _frontmatter(page: store.Page) -> dict:
    return {
        "id": page.id, "title": page.title, "created": page.created, "updated": page.updated,
        "sources": page.sources, "source_count": page.source_count,
        "last_confirmed": page.last_confirmed, "tags": page.tags, "kind": page.kind,
        "status": page.status, "supersedes": page.supersedes, "superseded_by": page.superseded_by,
        "contradicts": page.contradicts, "decay_class": page.decay_class, "question": page.question,
    }


def _hit_dict(h: search.SearchHit) -> dict:
    return {
        "id": h.id, "title": h.title, "snippet": h.snippet,
        "bm25_score": h.bm25_score, "confidence": round(h.confidence, 4),
        "graph_proximity": round(h.graph_proximity, 4), "final_score": round(h.final_score, 4),
        "status": h.status, "grounding": h.grounding,
    }


def _edge_dict(e: dict) -> dict:
    return {
        "s": e["s"], "p": e["p"], "o": e["o"], "confidence": round(e["confidence"], 4),
        "assertion_count": e["assertion_count"], "demoted": e["demoted"],
        "source_pages": e["source_pages"],
    }


# --- REST handlers ----------------------------------------------------------


async def _list_pages(request: Request) -> JSONResponse:
    qp = request.query_params
    pages = store.list_pages(status=qp.get("status") or None, kind=qp.get("kind") or None)
    q = (qp.get("q") or "").strip().lower()
    if q:
        pages = [
            p for p in pages
            if q in p.title.lower() or q in p.id.lower() or any(q in t.lower() for t in p.tags)
        ]
    summaries = [_page_summary(p) for p in pages]
    return JSONResponse({"pages": summaries, "total": len(summaries)})


async def _get_page(request: Request) -> JSONResponse:
    pid = request.path_params["page_id"]
    try:
        page = store.read_page(pid)
    except (FileNotFoundError, ValueError):
        return JSONResponse({"error": f"no such page: {pid}"}, status_code=404)
    score, breakdown = confidence.compute_confidence(page, access=state.get_access(pid))
    raw = (config.PAGES_DIR / f"{pid}.md").read_text(encoding="utf-8")
    return JSONResponse({
        "id": page.id,
        "frontmatter": _frontmatter(page),
        "body": page.body,
        "raw": raw,
        "confidence": round(score, 4),
        "breakdown": {k: (round(v, 6) if isinstance(v, float) else v) for k, v in breakdown.items()},
        "relations": page.relations,
        "supersedes": page.supersedes,
        "superseded_by": page.superseded_by,
        "contradicts": page.contradicts,
        "open_contradiction": pid in _open_contradiction_ids(),
    })


async def _search(request: Request) -> JSONResponse:
    qp = request.query_params
    q = qp.get("q", "")
    try:
        limit = int(qp.get("limit", 10))
    except ValueError:
        limit = 10
    hits = graph.graph_query(q, limit=limit) if q else []
    return JSONResponse({"query": q, "hits": [_hit_dict(h) for h in hits]})


def _build_subgraph(root: str | None, depth: int, include_demoted: bool) -> dict:
    backend = graph.get_graph_backend()
    edges = backend.all_edges()
    if not include_demoted:
        edges = [e for e in edges if not e["demoted"]]
    types = {e["ref"]: e["type"] for e in backend.all_entities()}

    if root:
        adj: dict[str, set[str]] = {}
        for e in edges:
            adj.setdefault(e["s"], set()).add(e["o"])
            adj.setdefault(e["o"], set()).add(e["s"])
        seen, frontier = {root}, {root}
        for _ in range(max(0, depth)):
            nxt: set[str] = set()
            for r in frontier:
                for n in adj.get(r, ()):
                    if n not in seen:
                        seen.add(n)
                        nxt.add(n)
            frontier = nxt
        node_refs = seen
    else:
        # Overview: prefer high-degree entities, bounded.
        deg: dict[str, int] = {}
        for e in edges:
            deg[e["s"]] = deg.get(e["s"], 0) + 1
            deg[e["o"]] = deg.get(e["o"], 0) + 1
        node_refs = set(sorted(deg, key=lambda r: (-deg[r], r))[:_MAX_OVERVIEW_NODES])

    sub_edges = [e for e in edges if e["s"] in node_refs and e["o"] in node_refs]

    if len(node_refs) > _MAX_NODES:  # hard cap, keeping the root + highest-degree
        deg = {}
        for e in sub_edges:
            deg[e["s"]] = deg.get(e["s"], 0) + 1
            deg[e["o"]] = deg.get(e["o"], 0) + 1
        keep = set(sorted(node_refs, key=lambda r: (-deg.get(r, 0), r))[:_MAX_NODES])
        if root:
            keep.add(root)
        node_refs = keep
        sub_edges = [e for e in sub_edges if e["s"] in node_refs and e["o"] in node_refs]

    fdeg: dict[str, int] = {}
    for e in sub_edges:
        fdeg[e["s"]] = fdeg.get(e["s"], 0) + 1
        fdeg[e["o"]] = fdeg.get(e["o"], 0) + 1
    nodes = [{"ref": r, "type": types.get(r, "?"), "degree": fdeg.get(r, 0)} for r in sorted(node_refs)]
    return {
        "root": root, "depth": depth, "include_demoted": include_demoted,
        "nodes": nodes, "edges": [_edge_dict(e) for e in sub_edges],
    }


async def _graph(request: Request) -> JSONResponse:
    qp = request.query_params
    try:
        depth = int(qp.get("depth", 2))
    except ValueError:
        depth = 2
    include_demoted = (qp.get("include_demoted") or "").lower() in ("1", "true", "yes")
    return JSONResponse(_build_subgraph(qp.get("root") or None, depth, include_demoted))


async def _entity(request: Request) -> JSONResponse:
    ref = request.path_params["ref"]
    ent = graph.entity(ref)
    if ent is None:
        return JSONResponse({"error": f"no such entity: {ref}"}, status_code=404)
    return JSONResponse({"ref": ref, **ent})


async def _impact(request: Request) -> JSONResponse:
    ref = request.path_params["ref"]
    try:
        depth = int(request.query_params.get("depth", 3))
    except ValueError:
        depth = 3
    return JSONResponse({"entity": ref, "affected": graph.impact(ref, depth=depth)})


async def _fileback(request: Request) -> JSONResponse:
    from . import mcp_server  # reuse the exact file-back path (lazy: avoids import cycle)

    body = await request.json()
    question = (body.get("question") or "").strip()
    answer = (body.get("answer") or "").strip()
    if not question or not answer:
        return JSONResponse({"error": "question and answer are required"}, status_code=400)
    result = mcp_server.mnesis_file_back(question, answer)
    if result.startswith("filed digest:"):
        digest_id = result[len("filed digest:"):].split("(")[0].strip()
        return JSONResponse({"filed": True, "digest_id": digest_id, "message": result})
    return JSONResponse({"filed": False, "digest_id": None, "reason": result})


# --- Chat (SSE, grounded) ---------------------------------------------------


def _chunks(text: str):
    """Token-ish chunks for incremental SSE flushing (no whole-answer buffering)."""
    for word in text.split(" "):
        yield word + " "


def _grounded_answer(message: str, pages: list[store.Page]) -> str:
    if config.MNESIS_LLM_STUB:
        # Deterministic, offline: ground in the top page and cite all retrieved ids.
        cites = " ".join(f"[[{p.id}]]" for p in pages)
        return f"Based on the wiki: {pages[0].title} {cites}"
    context = "\n\n".join(f"[[{p.id}]] {p.title}\n{p.body}" for p in pages)
    user = f"Question: {message}\n\nPAGES:\n{context}"
    return llm.complete(GROUNDED_SYSTEM_PROMPT, user)


async def _chat(request: Request) -> EventSourceResponse:
    body = await request.json()
    message = (body.get("message") or "").strip()

    hits = graph.graph_query(message, limit=CHAT_TOP_N) if message else []
    pages: list[store.Page] = []
    for h in hits:
        try:
            pages.append(store.read_page(h.id))
        except (FileNotFoundError, ValueError):
            continue

    async def stream():
        if not pages:
            # Never answer from model memory: no pages -> say so, zero citations.
            text = "The wiki does not contain information to answer that question."
            for chunk in _chunks(text):
                yield {"event": "token", "data": chunk}
            yield {"event": "done", "data": json.dumps({"citations": [], "retrieval": []})}
            return

        answer = _grounded_answer(message, pages)
        for chunk in _chunks(answer):
            yield {"event": "token", "data": chunk}

        valid = {p.id for p in pages}
        cited = list(dict.fromkeys(c for c in _CITE_RE.findall(answer) if c in valid))
        retrieval = [{"id": h.id, "final_score": round(h.final_score, 4)} for h in hits]
        yield {"event": "done", "data": json.dumps({"citations": cited, "retrieval": retrieval})}

    return EventSourceResponse(stream())


# --- Mounting ---------------------------------------------------------------

API_ROUTES = [
    Route("/api/pages", _list_pages, methods=["GET"]),
    Route("/api/pages/{page_id}", _get_page, methods=["GET"]),
    Route("/api/search", _search, methods=["GET"]),
    Route("/api/graph", _graph, methods=["GET"]),
    Route("/api/entity/{ref:path}", _entity, methods=["GET"]),
    Route("/api/impact/{ref:path}", _impact, methods=["GET"]),
    Route("/api/chat", _chat, methods=["POST"]),
    Route("/api/fileback", _fileback, methods=["POST"]),
]


def mount_api(app) -> None:
    """Append the /api routes to an existing Starlette app (the MCP HTTP app)."""
    app.router.routes.extend(API_ROUTES)
