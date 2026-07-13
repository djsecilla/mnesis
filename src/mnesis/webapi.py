"""Browser-friendly REST + SSE gateway for the web UI.

Mounted under ``/api`` in the **same** Starlette app the MCP server serves (see
``mcp_server.build_http_app``), so it reuses one process and port. Authentication is the
IAM5 **web session** (cookie + CSRF + the PDP, via ``webauth.WebSessionMiddleware``);
``/mcp`` uses per-agent keys (IAM7) and ``/health`` stays open.

These routes are **thin adapters** — no business logic lives here that isn't in
``store`` / ``search`` / ``graph`` / ``confidence`` / ``mcp_server``. All graph
access goes through the ``GraphBackend`` interface (via ``graph.*``).

Chat is grounded: it answers ONLY from retrieved wiki pages, cites them inline as
``[[page-id]]``, and says so (with zero citations) when the wiki has nothing.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from sse_starlette.sse import EventSourceResponse

from . import auth, authz, config, confidence, graph, ingest, llm, okf, okf_bundle, search, state, store, tenancy, vocab

log = logging.getLogger(__name__)

# How many retrieved pages ground a chat answer.
CHAT_TOP_N = 5


def _refresh_graph() -> None:
    """Rebuild the graph cache after a UI write so newly-ingested entities and
    relations (and supersession demotions) show up in the graph view. Ingest
    updates the search index incrementally but not the graph; the graph is
    otherwise only rebuilt by ``mnesis rebuild``. Best-effort: a failure here
    never fails the write — the page is already committed and a later
    ``rebuild`` recovers the graph."""
    try:
        graph.rebuild_graph()
    except Exception:  # noqa: BLE001 — never let cache refresh break a committed write
        log.warning("graph rebuild after write failed; run `mnesis rebuild`", exc_info=True)
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


# --- visibility (T4/T5): every gateway read is scoped to the bound principal ---
# When no principal is bound (legacy single-tenant) nothing is narrowed.


def _visible_pages(status: str | None = None, kind: str | None = None) -> list[store.Page]:
    principal = auth.current_principal_or_none()
    pages = store.list_pages(status=status, kind=kind)
    return pages if principal is None else [p for p in pages if authz.can_see(principal, p)]


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


def _okf_core(page: store.Page) -> dict:
    """The OKF-core view of a page (OKF6) — additive to the existing `frontmatter` block,
    so existing consumers are unaffected. `concept_id` is the OKF path identity."""
    m = okf.to_okf_metadata(page)
    return {
        "concept_id": page.id,      # OKF identity = the bundle path (pages/<id>)
        "type": m["type"],
        "title": m["title"],
        "description": m["description"],
        "resource": m.get("resource"),   # None: Mnesis concepts are abstract (provenance = sources)
        "tags": m["tags"],
        "timestamp": m["timestamp"],
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
        "source_pages": e["source_pages"], "symmetric": e.get("symmetric", False),
    }


# --- REST handlers ----------------------------------------------------------


async def _list_pages(request: Request) -> JSONResponse:
    authz.require_permission(authz.READ)
    qp = request.query_params
    pages = _visible_pages(status=qp.get("status") or None, kind=qp.get("kind") or None)
    q = (qp.get("q") or "").strip().lower()
    if q:
        pages = [
            p for p in pages
            if q in p.title.lower() or q in p.id.lower() or any(q in t.lower() for t in p.tags)
        ]
    summaries = [_page_summary(p) for p in pages]
    return JSONResponse({"pages": summaries, "total": len(summaries)})


async def _get_page(request: Request) -> JSONResponse:
    authz.require_permission(authz.READ)
    pid = request.path_params["page_id"]
    try:
        page = store.read_page(pid)
    except (FileNotFoundError, ValueError):
        return JSONResponse({"error": f"no such page: {pid}"}, status_code=404)
    # Visibility (T5): a private page is reported absent to a non-owner (no leak).
    if not authz.page_visible_to_active(page):
        return JSONResponse({"error": f"no such page: {pid}"}, status_code=404)
    score, breakdown = confidence.compute_confidence(page, access=state.get_access(pid))
    raw = (tenancy.current().pages_dir / f"{pid}.md").read_text(encoding="utf-8")
    return JSONResponse({
        "id": page.id,
        "frontmatter": _frontmatter(page),
        "okf": _okf_core(page),          # OKF6: additive OKF-core fields (path id + type/description/…)
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
    authz.require_permission(authz.READ)
    qp = request.query_params
    q = qp.get("q", "")
    try:
        limit = int(qp.get("limit", 10))
    except ValueError:
        limit = 10
    hits = graph.graph_query(q, limit=limit) if q else []
    return JSONResponse({"query": q, "hits": [_hit_dict(h) for h in hits]})


def _entity_mentions() -> dict[str, int]:
    """Per-entity ``mentions`` = the number of DISTINCT pages that reference the
    entity, either via an entity-typed ``type:value`` tag or as the subject/object
    of a relation. A corpus-wide "how much of the knowledge base is about this
    entity" signal (a citation/occurrence count) — stable across graph views,
    unlike edge degree. Drives node size in the UI.
    """
    counts: dict[str, int] = {}
    for page in _visible_pages():
        refs: set[str] = set()
        for tag in page.tags:
            try:
                refs.add(vocab.normalize_ref(tag))  # entity-typed tags only
            except ValueError:
                pass  # free tag (not an entity ref) — ignored
        for rel in page.relations:
            if {"s", "o"} <= rel.keys():
                refs.update((rel["s"], rel["o"]))
        for ref in refs:
            counts[ref] = counts.get(ref, 0) + 1  # one increment per distinct page
    return counts


def _build_subgraph(root: str | None, depth: int, include_demoted: bool) -> dict:
    backend = graph.get_graph_backend()
    edges = backend.all_edges()
    if not include_demoted:
        edges = [e for e in edges if not e["demoted"]]
    # Visibility (T5): drop edges asserted only by pages the principal cannot see,
    # and nodes not backed by any visible page — the graph view never shows a
    # private-only entity/edge.
    visible_ids = authz.active_visible_page_ids()
    visible_refs = authz.active_visible_entity_refs()
    if visible_ids is not None:
        edges = [e for e in edges if any(sp in visible_ids for sp in e["source_pages"])]
    types = {
        e["ref"]: e["type"]
        for e in backend.all_entities()
        if visible_refs is None or e["ref"] in visible_refs
    }
    mentions = _entity_mentions()

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
        # Overview: rank ALL entities by edge-degree, then take the top N. Edge-
        # connected entities come first; if there is room (or no edges at all),
        # isolated entities are included too — so a knowledge base whose pages
        # carry entity tags but no relations still renders nodes rather than a
        # misleading "No graph yet" blank.
        deg: dict[str, int] = {}
        for e in edges:
            deg[e["s"]] = deg.get(e["s"], 0) + 1
            deg[e["o"]] = deg.get(e["o"], 0) + 1
        ranked = sorted(types, key=lambda r: (-deg.get(r, 0), r))
        node_refs = set(ranked[:_MAX_OVERVIEW_NODES])

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
    nodes = [
        {"ref": r, "type": types.get(r, "?"), "degree": fdeg.get(r, 0), "mentions": mentions.get(r, 0)}
        for r in sorted(node_refs)
    ]
    return {
        "root": root, "depth": depth, "include_demoted": include_demoted,
        "nodes": nodes, "edges": [_edge_dict(e) for e in sub_edges],
    }


async def _graph(request: Request) -> JSONResponse:
    authz.require_permission(authz.READ)
    qp = request.query_params
    try:
        depth = int(qp.get("depth", 2))
    except ValueError:
        depth = 2
    include_demoted = (qp.get("include_demoted") or "").lower() in ("1", "true", "yes")
    return JSONResponse(_build_subgraph(qp.get("root") or None, depth, include_demoted))


def _first_paragraph(body: str, limit: int) -> str:
    """First prose paragraph of a page body (skips the trailing 'Source:' line)."""
    for para in body.split("\n\n"):
        p = " ".join(para.split())
        if p and not p.lower().startswith("source:"):
            return p[:limit]
    return ""


def _is_entity_tag(tag: str) -> bool:
    i = tag.find(":")
    return i > 0 and tag[:i] in vocab.active_config().entity_types


async def _entity(request: Request) -> JSONResponse:
    """Panel-ready entity detail in ONE call: summary, ranked sources (provenance),
    co-occurring entity tags, and typed-edge neighbours. Thin — reuses graph +
    store + confidence; `pages`/`edges` are kept for back-compat (the page reader
    looks up edge confidence via this endpoint)."""
    authz.require_permission(authz.READ)
    ref = request.path_params["ref"]
    ent = graph.entity(ref)
    if ent is None:
        return JSONResponse({"error": f"no such entity: {ref}"}, status_code=404)

    # Declaring/mentioning pages: any page tagging this entity (a superset of the
    # edge source-pages, since ingest tags every edge endpoint). Ranked by confidence.
    declaring = [p for p in _visible_pages() if ref in p.tags]
    declaring.sort(key=_conf, reverse=True)

    active = [p for p in declaring if p.status == "active"]
    summary = _first_paragraph(active[0].body, 280) if active else ""

    sources = [
        {"id": p.id, "title": p.title, "kind": p.kind,
         "confidence": round(_conf(p), 4), "snippet": _first_paragraph(p.body, 140)}
        for p in declaring[:8]
    ]

    # Co-occurring entity tags across declaring pages, top ~8 by frequency.
    counts: Counter = Counter()
    for p in declaring:
        for t in p.tags:
            if t != ref and _is_entity_tag(t):
                counts[t] += 1
    tags = [t for t, _ in counts.most_common(8)]

    # Typed-edge neighbours (demoted excluded), each with predicate + direction.
    related = []
    for e in ent["edges"]:
        if e["demoted"]:
            continue
        if e["s"] == ref:
            other, direction = e["o"], "out"
        elif e["o"] == ref:
            other, direction = e["s"], "in"
        else:
            continue
        otype = other.split(":", 1)[0] if ":" in other else "concept"
        related.append({"ref": other, "type": otype, "predicate": e["p"],
                        "direction": direction, "confidence": round(e["confidence"], 4)})
    related = related[:12]

    return JSONResponse({
        "ref": ref,
        "type": ent["type"],
        "confidence": round(_conf(declaring[0]), 4) if declaring else None,
        "summary": summary,
        "sources": sources,
        "tags": tags,
        "related": related,
        "pages": ent["pages"],   # back-compat
        "edges": ent["edges"],   # back-compat (page reader reads edge confidence)
    })


async def _impact(request: Request) -> JSONResponse:
    authz.require_permission(authz.READ)
    ref = request.path_params["ref"]
    try:
        depth = int(request.query_params.get("depth", 3))
    except ValueError:
        depth = 3
    return JSONResponse({"entity": ref, "affected": graph.impact(ref, depth=depth)})


async def _fileback(request: Request) -> JSONResponse:
    from . import mcp_server  # reuse the exact file-back path (lazy: avoids import cycle)

    authz.require_permission(authz.WRITE)
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
        # Deterministic, offline: a developed answer that restates each retrieved
        # page's claim (so it is genuinely grounded — every sentence is cited) and
        # is long enough to clear the file-back quality heuristic, exercising the
        # full compounding loop offline.
        lead = pages[0].title.rstrip(".")
        sentences = [f"Based on the wiki, {lead} [[{pages[0].id}]]."]
        for p in pages[1:]:
            sentences.append(f"Relatedly, {p.title.rstrip('.').lower()} [[{p.id}]].")
        sentences.append("This answer is synthesized only from the cited wiki pages above.")
        return " ".join(sentences)
    context = "\n\n".join(f"[[{p.id}]] {p.title}\n{p.body}" for p in pages)
    user = f"Question: {message}\n\nPAGES:\n{context}"
    return llm.complete(GROUNDED_SYSTEM_PROMPT, user)


async def _chat(request: Request) -> EventSourceResponse:
    authz.require_permission(authz.READ)
    body = await request.json()
    message = (body.get("message") or "").strip()

    hits = graph.graph_query(message, limit=CHAT_TOP_N) if message else []
    pages: list[store.Page] = []
    for h in hits:
        try:
            pages.append(store.read_page(h.id))
        except (FileNotFoundError, ValueError):
            continue

    # Build the grounding (with component scores) once — used by both the answer
    # and the LLM-unavailable paths so the user always sees what was retrieved.
    page_by_id = {p.id: p for p in pages}
    retrieval = [
        {
            "id": h.id,
            "title": h.title,
            "kind": page_by_id[h.id].kind,
            "status": h.status,
            "confidence": round(h.confidence, 4),
            "bm25_score": round(h.bm25_score, 4),
            "graph_proximity": round(h.graph_proximity, 4),
            "final_score": round(h.final_score, 4),
        }
        for h in hits
        if h.id in page_by_id
    ]

    async def stream():
        if not pages:
            # Never answer from model memory: no pages -> say so, zero citations.
            text = "The wiki does not contain information to answer that question."
            for chunk in _chunks(text):
                yield {"event": "token", "data": chunk}
            yield {"event": "done", "data": json.dumps({"citations": [], "retrieval": []})}
            return

        try:
            # Blocking LLM call — off the event loop so the SSE stream and the
            # rest of the server stay responsive while the answer is generated.
            answer = await run_in_threadpool(_grounded_answer, message, pages)
        except Exception:  # noqa: BLE001 — degrade gracefully on any LLM failure
            # The model is unreachable (no API credits, network, etc.). Don't crash
            # the stream: tell the user plainly, and still surface the retrieved
            # pages so it's clear the wiki data WAS considered.
            log.warning("grounded answer failed; LLM unavailable", exc_info=True)
            note = (
                "Found relevant pages in the wiki, but the configured answer model "
                "is currently unavailable, so there is no synthesized answer. The "
                "grounding below shows what was retrieved."
            )
            for chunk in _chunks(note):
                yield {"event": "token", "data": chunk}
            yield {"event": "done", "data": json.dumps(
                {"citations": [], "retrieval": retrieval, "error": "llm_unavailable"})}
            return

        for chunk in _chunks(answer):
            yield {"event": "token", "data": chunk}
        valid = set(page_by_id)
        cited = list(dict.fromkeys(c for c in _CITE_RE.findall(answer) if c in valid))
        yield {"event": "done", "data": json.dumps({"citations": cited, "retrieval": retrieval})}

    return EventSourceResponse(stream())


# --- Ingestion (plan/apply over HTTP) ---------------------------------------


def _err(code: str, message: str, status: int) -> JSONResponse:
    """Structured error the UI can render: ``{code, message}``."""
    return JSONResponse({"code": code, "message": message}, status_code=status)


class _IngestInputError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        self.code, self.message, self.status = code, message, status


# Content-type -> text extractor. text/* is handled now; this is the extension
# point for richer types: register a PDF/DOCX extractor here when added. Until
# then those types are rejected with a friendly message (see ``_UNSUPPORTED``).
def _extract_textlike(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


_TEXT_EXTRACTORS: dict[str, callable] = {
    "text/markdown": _extract_textlike,
    "text/x-markdown": _extract_textlike,
    "text/plain": _extract_textlike,
}
_UNSUPPORTED: dict[str, str] = {
    "application/pdf": "PDF",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "DOCX",
    "application/msword": "DOC",
}


def _extract_upload(content_type: str | None, data: bytes) -> str:
    """Dispatch an uploaded file to a text extractor by content-type."""
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in _UNSUPPORTED:
        raise _IngestInputError(
            "unsupported_type",
            f"{_UNSUPPORTED[ct]} upload is not supported yet — only text/markdown for now.",
            status=415,
        )
    if ct in _TEXT_EXTRACTORS:
        return _TEXT_EXTRACTORS[ct](data)
    if ct.startswith("text/") or ct in ("", "application/octet-stream"):
        return _extract_textlike(data)  # treat unknown/plain blobs as text
    raise _IngestInputError(
        "unsupported_type", f"unsupported upload type: {ct} — only text/markdown for now.", status=415
    )


def _safe_source_ref(raw: str | None, fallback: str) -> str:
    """A filesystem/git-safe source ref from a user value or filename stem."""
    slug = store.slugify((raw or "").strip() or (fallback or "").strip())
    if not slug:
        slug = "source-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    return slug


async def _read_ingest_input(request: Request) -> tuple[str, str]:
    """Resolve ``(text, source_ref)`` from a JSON body or a multipart upload,
    enforcing the max-upload size and validating the content type."""
    max_bytes = config.MNESIS_MAX_UPLOAD_BYTES
    ctype = request.headers.get("content-type", "")

    if ctype.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            raise _IngestInputError("missing_file", "multipart upload requires a 'file' part")
        data = await upload.read()
        if len(data) > max_bytes:
            raise _IngestInputError(
                "payload_too_large", f"upload is {len(data)} bytes; limit is {max_bytes}", status=413
            )
        text = _extract_upload(getattr(upload, "content_type", None), data)
        ref = _safe_source_ref(form.get("source_ref"), Path(getattr(upload, "filename", "") or "").stem)
        return text, ref

    try:
        body = await request.json()
    except Exception:
        raise _IngestInputError("invalid_json", "request body must be JSON or multipart/form-data")
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        raise _IngestInputError("missing_text", "'text' is required")
    if len(text.encode("utf-8")) > max_bytes:
        raise _IngestInputError(
            "payload_too_large", f"text is {len(text.encode('utf-8'))} bytes; limit is {max_bytes}", status=413
        )
    return text, _safe_source_ref(body.get("source_ref"), "pasted")


def _llm_err(exc: Exception) -> JSONResponse:
    """Turn an extraction/LLM failure into a clean, structured 502.

    A timeout or connection error to the model would otherwise surface as a bare
    500 (plain text), which the client can't parse. This keeps the contract JSON
    and tells the user something actionable.
    """
    import httpx

    log.warning("ingest extraction failed", exc_info=True)
    if isinstance(exc, httpx.TimeoutException):
        return _err(
            "llm_timeout",
            "The extraction model timed out. Try a shorter source, raise "
            "MNESIS_LLM_TIMEOUT, or use a faster model.",
            502,
        )
    if isinstance(exc, httpx.HTTPError):
        return _err(
            "llm_unavailable",
            "Could not reach the extraction model. Check the LLM provider/endpoint.",
            502,
        )
    return _err("extraction_failed", f"Extraction failed: {exc}", 502)


async def _ingest_preview(request: Request) -> JSONResponse:
    """Side-effect-free preview: returns the IngestPlan (calls plan_ingest only)."""
    authz.require_permission(authz.WRITE)
    try:
        text, ref = await _read_ingest_input(request)
    except _IngestInputError as e:
        return _err(e.code, e.message, e.status)
    try:
        # plan_ingest does a blocking LLM call (and is read-only — no writes), so
        # run it off the event loop to keep the server (and /health) responsive
        # during a slow local-model extraction.
        plan = await run_in_threadpool(ingest.plan_ingest, text, ref)
    except Exception as e:  # LLM timeout / unreachable / extraction failure
        return _llm_err(e)
    return JSONResponse(plan)


async def _ingest_commit(request: Request) -> JSONResponse:
    """Apply a previously previewed plan (+ optional overrides): returns the result."""
    authz.require_permission(authz.WRITE)
    try:
        body = await request.json()
    except Exception:
        return _err("invalid_json", "request body must be JSON", 400)
    plan = body.get("plan")
    if not isinstance(plan, dict) or "draft_page" not in plan or "source_ref" not in plan:
        return _err("invalid_plan", "a valid ingest plan is required", 400)
    overrides = body.get("overrides")
    try:
        result = ingest.apply_ingest(plan, overrides if isinstance(overrides, dict) else None)
    except ValueError as e:
        return _err("invalid_override", str(e), 400)
    except Exception as e:  # LLM timeout / unreachable / extraction failure
        return _llm_err(e)
    _refresh_graph()  # so the new page's entities/relations appear in the graph
    return JSONResponse(result)


# --- Sources (provenance) ---------------------------------------------------


def _pages_by_source() -> dict[str, list[store.Page]]:
    # Visibility (T5): only pages the principal may see — a source whose only pages
    # are private to someone else is absent from listings/detail entirely.
    mapping: dict[str, list[store.Page]] = {}
    for p in _visible_pages():
        for ref in p.sources:
            mapping.setdefault(ref, []).append(p)
    return mapping


def _source_ingested_at(path: Path) -> str | None:
    """The git add-time of the source file (its ingestion), with an mtime fallback."""
    try:
        out = subprocess.run(
            ["git", "-C", str(tenancy.current().sources_dir), "log", "--diff-filter=A", "-1",
             "--format=%cI", "--", str(path)],
            capture_output=True, text=True, timeout=5,
        )
        if out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        return None


async def _list_sources(request: Request) -> JSONResponse:
    authz.require_permission(authz.READ)
    by_source = _pages_by_source()
    sources_dir = tenancy.current().sources_dir
    sources_dir.mkdir(parents=True, exist_ok=True)
    items = []
    for path in sorted(sources_dir.glob("*.md")):
        ref = path.stem
        visible_pages = by_source.get(ref, [])
        # A source backed only by pages the principal can't see is not listed.
        if auth.current_principal_or_none() is not None and not visible_pages:
            continue
        items.append({
            "id": ref,
            "ingested_at": _source_ingested_at(path),
            "pages": [{"id": p.id, "title": p.title} for p in visible_pages],
        })
    return JSONResponse({"sources": items, "total": len(items)})


async def _get_source(request: Request) -> JSONResponse:
    authz.require_permission(authz.READ)
    ref = request.path_params["source_id"]
    if "/" in ref or "\\" in ref or ref in {"", ".", ".."}:
        return _err("invalid_source", "invalid source id", 400)
    path = tenancy.current().sources_dir / f"{ref}.md"
    if not path.exists():
        return _err("not_found", f"no such source: {ref}", 404)
    by_source = _pages_by_source()
    # Visibility (T5): hide a source whose only pages are private to someone else.
    if auth.current_principal_or_none() is not None and not by_source.get(ref):
        return _err("not_found", f"no such source: {ref}", 404)
    # The stored text was redacted at ingest time, so it carries no raw values.
    return JSONResponse({
        "id": ref,
        "ingested_at": _source_ingested_at(path),
        "text": path.read_text(encoding="utf-8"),
        "pages": [{"id": p.id, "title": p.title} for p in by_source.get(ref, [])],
    })


# --- Reviews (contradiction queue) ------------------------------------------


def _review_page(pid: str) -> dict:
    try:
        page = store.read_page(pid)
        return {"id": pid, "title": page.title, "confidence": round(_conf(page), 4)}
    except (FileNotFoundError, ValueError):
        return {"id": pid, "title": None, "confidence": None}


def _review_visible(r: dict) -> bool:
    """A review is shown only if the principal can see BOTH referenced pages
    (so a private page never surfaces via the review queue)."""
    principal = auth.current_principal_or_none()
    if principal is None:
        return True
    for pid in (r["page_a"], r["page_b"]):
        try:
            if not authz.can_see(principal, store.read_page(pid)):
                return False
        except (FileNotFoundError, ValueError):
            continue
    return True


async def _list_reviews(request: Request) -> JSONResponse:
    authz.require_permission(authz.READ)
    reviews = [
        {
            "id": r["id"],
            "page_a": _review_page(r["page_a"]),
            "page_b": _review_page(r["page_b"]),
            "detail": r["detail"],
        }
        for r in state.list_open_reviews()
        if _review_visible(r)
    ]
    return JSONResponse({"reviews": reviews, "total": len(reviews)})


async def _resolve_review(request: Request) -> JSONResponse:
    from . import mcp_server  # reuse the exact resolve path (lazy: avoids import cycle)

    authz.require_permission(authz.MAINTAIN)
    try:
        review_id = int(request.path_params["review_id"])
    except (ValueError, TypeError):
        return _err("invalid_review", "review id must be an integer", 400)
    try:
        body = await request.json()
    except Exception:
        body = {}
    keep = (body.get("keep_page_id") or "").strip()
    if not keep:
        return _err("missing_keep", "keep_page_id is required", 400)

    msg = mcp_server.mnesis_resolve(review_id, keep)
    if msg.startswith("resolved review"):
        superseded = msg.rsplit("superseded ", 1)[-1].strip() if "superseded " in msg else None
        _refresh_graph()  # the superseded page's edges are now demoted
        return JSONResponse(
            {"resolved": True, "review_id": review_id, "kept": keep,
             "superseded": superseded, "message": msg}
        )
    if msg.startswith("no open review"):
        return _err("not_found", msg, 404)
    return _err("invalid_keep", msg, 400)


async def _admin_credentials(request: Request) -> JSONResponse:
    """Admin-only: list the tenant's credentials (no secrets). Gated by the PDP on
    ``credentials:issue`` — a member/readonly gets 403, a tenant-admin gets the list."""
    authz.require_permission(authz.CREDENTIALS_ISSUE)
    ctx = tenancy.current()
    creds = auth.CredentialStore().list_for_tenant(ctx.tenant_id)
    return JSONResponse({"credentials": [c.public_dict() for c in creds], "total": len(creds)})


# --- OKF interop (export / import / concept) --------------------------------


async def _okf_concept(request: Request) -> JSONResponse:
    """The OKF-conformant concept document + its OKF-core fields (path identity)."""
    authz.require_permission(authz.READ)
    pid = request.path_params["page_id"]
    try:
        page = store.read_page(pid)
    except (FileNotFoundError, ValueError):
        return _err("not_found", f"no such concept: {pid}", 404)
    if not authz.page_visible_to_active(page):
        return _err("not_found", f"no such concept: {pid}", 404)
    return JSONResponse({"concept_id": page.id, "okf": _okf_core(page),
                         "document": okf.to_okf_document(page)})


async def _okf_export(request: Request) -> "Response":
    """Export this tenant's knowledge as a conformant OKF **bundle** (`.tar.gz` download)."""
    from starlette.responses import FileResponse

    authz.require_permission(authz.READ)
    rep = okf_bundle.export_bundle(fmt="tar")
    tenant = tenancy.current().tenant_id
    return FileResponse(rep["path"], media_type="application/gzip",
                        filename=f"{tenant}-okf-bundle.tar.gz")


async def _okf_import(request: Request) -> JSONResponse:
    """Import an external OKF bundle (multipart `.tar.gz`) **through the governed ingest
    path** — redaction, routing, and review apply; bundle content is UNTRUSTED data."""
    import tempfile
    from pathlib import Path as _Path

    authz.require_permission(authz.WRITE)
    ctype = request.headers.get("content-type", "")
    if not ctype.startswith("multipart/form-data"):
        return _err("invalid_upload", "OKF import requires a multipart 'file' upload (a .tar.gz bundle)", 400)
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        return _err("missing_file", "multipart upload requires a 'file' part", 400)
    data = await upload.read()
    if len(data) > config.MNESIS_MAX_UPLOAD_BYTES:
        return _err("payload_too_large", f"bundle is {len(data)} bytes; limit is {config.MNESIS_MAX_UPLOAD_BYTES}", 413)
    tmp = _Path(tempfile.mkdtemp(prefix="okf-upload-"))
    try:
        bundle = tmp / "bundle.tar.gz"
        bundle.write_bytes(data)
        try:
            rep = await run_in_threadpool(okf_bundle.import_bundle, bundle)
        except ValueError as exc:
            return _err("invalid_bundle", str(exc), 400)
        except Exception as exc:  # LLM/extraction failure during governed ingest
            return _llm_err(exc)
    finally:
        import shutil as _shutil
        _shutil.rmtree(tmp, ignore_errors=True)
    _refresh_graph()
    return JSONResponse(rep)


async def _config(_request: Request) -> JSONResponse:
    """Non-sensitive runtime config the UI adapts to. Notably the LLM provider,
    so the batch UI can default to sequential processing on a (slow) local model,
    and the per-extraction timeout it should expect."""
    return JSONResponse({
        "llm_provider": config.MNESIS_LLM_PROVIDER,
        "llm_stub": bool(config.MNESIS_LLM_STUB),
        "llm_timeout_seconds": config.MNESIS_LLM_TIMEOUT,
    })


# --- Mounting ---------------------------------------------------------------

API_ROUTES = [
    Route("/api/config", _config, methods=["GET"]),
    Route("/api/pages", _list_pages, methods=["GET"]),
    Route("/api/pages/{page_id}", _get_page, methods=["GET"]),
    Route("/api/search", _search, methods=["GET"]),
    Route("/api/graph", _graph, methods=["GET"]),
    Route("/api/entity/{ref:path}", _entity, methods=["GET"]),
    Route("/api/impact/{ref:path}", _impact, methods=["GET"]),
    Route("/api/chat", _chat, methods=["POST"]),
    Route("/api/fileback", _fileback, methods=["POST"]),
    Route("/api/ingest/preview", _ingest_preview, methods=["POST"]),
    Route("/api/ingest/commit", _ingest_commit, methods=["POST"]),
    Route("/api/sources", _list_sources, methods=["GET"]),
    Route("/api/sources/{source_id}", _get_source, methods=["GET"]),
    Route("/api/reviews", _list_reviews, methods=["GET"]),
    Route("/api/reviews/{review_id}/resolve", _resolve_review, methods=["POST"]),
    Route("/api/admin/credentials", _admin_credentials, methods=["GET"]),
    Route("/api/okf/concept/{page_id}", _okf_concept, methods=["GET"]),
    Route("/api/okf/export", _okf_export, methods=["GET"]),
    Route("/api/okf/import", _okf_import, methods=["POST"]),
]


def mount_api(app) -> None:
    """Append the /api routes to an existing Starlette app (the MCP HTTP app)."""
    app.router.routes.extend(API_ROUTES)
