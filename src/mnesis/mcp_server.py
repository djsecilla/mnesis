"""FastMCP server exposing the mnesis wiki tools.

This is the agent-facing surface: Claude Code (and any MCP client) can ingest
sources, query the index, fetch pages, and file synthesized answers back. The
tools are thin orchestration over the core modules (filters, ingest, store,
search) — no business logic lives here that isn't in those modules.

Newly written pages are ``search.upsert``-ed into the index immediately, so a
``mnesis_file_back`` answer (or a fresh ingest) surfaces on the next
``mnesis_query`` — the compounding loop the PoC exists to demonstrate.

Two transports (selected by ``MNESIS_MCP_TRANSPORT``): **stdio** (default; local
Claude Code spawns it as a subprocess — unchanged, local trust) and **http**
(streamable HTTP for container deployment, with a ``GET /health`` endpoint). Over
HTTP every ``/mcp`` call authenticates with a per-tenant, per-principal **agent key**
(IAM7) and each tool enforces that key's scopes through the PDP — there is no global
token. Verified against mcp 1.27.x: ``FastMCP``, ``@mcp.tool()``, ``mcp.custom_route``,
``mcp.streamable_http_app()``, ``mcp.run()``.
"""

from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse

from . import (
    auth,
    authz,
    config,
    confidence,
    graph,
    graph_lint,
    ingest,
    lifecycle,
    maintenance,
    okf,
    okf_bundle,
    quotas,
    search,
    state,
    store,
    tenancy,
    tokens,
)
from .filters import scrub
from .store import Page

log = logging.getLogger(__name__)


# --- Explicit tool → scope mapping (IAM7) ----------------------------------
# Every mnesis_* tool call goes through the PDP with the credential's scopes, so
# effective access = role ∩ scope ∩ tenant ∩ visibility. A read-scoped agent key
# can query but not ingest; a maintenance-scoped key can decay but not write, etc.
# The coarse action maps to the fine permissions via the PDP's permission classes.
_TOOL_SCOPES: dict[str, str] = {
    # reads
    "mnesis_query": authz.READ, "mnesis_get": authz.READ, "mnesis_list": authz.READ,
    "mnesis_impact": authz.READ, "mnesis_entity": authz.READ, "mnesis_neighbors": authz.READ,
    "mnesis_traverse": authz.READ, "mnesis_graph_stats": authz.READ,
    "mnesis_health_report": authz.READ, "mnesis_find_duplicates": authz.READ,
    "mnesis_review": authz.READ,
    "mnesis_okf_concept": authz.READ, "mnesis_okf_export": authz.READ,
    # writes
    "mnesis_ingest": authz.WRITE, "mnesis_file_back": authz.WRITE,
    "mnesis_okf_import": authz.WRITE,
    # maintenance
    "mnesis_rebuild": authz.MAINTAIN, "mnesis_decay": authz.MAINTAIN,
    "mnesis_graph_lint": authz.MAINTAIN, "mnesis_resolve": authz.MAINTAIN,
}


def _authorize(tool: str) -> None:
    """Enforce the PDP for ``tool`` against the bound principal (its role ∩ scope).
    Raises :class:`~mnesis.authz.AuthorizationError` on denial (surfaced by FastMCP as a
    tool error). No principal bound (local stdio / in-process) → permitted."""
    authz.require_permission(_TOOL_SCOPES[tool])


def _transport_security():
    """Build TransportSecuritySettings from ``MNESIS_MCP_ALLOWED_HOSTS``.

    Returns ``None`` when the env is unset (keep FastMCP's secure default —
    localhost only). When set, DNS-rebinding protection stays ON but the listed
    hosts (and their http/https origins) are accepted, so a networked client
    that reaches the server by service name (``mnesis:8080``) is not rejected
    with 421. The python MCP client sends no Origin header, so origins matter
    only for browser callers.
    """
    raw = config.MNESIS_MCP_ALLOWED_HOSTS.strip()
    if not raw:
        return None
    hosts = [h.strip() for h in raw.split(",") if h.strip()]
    origins: list[str] = []
    for h in hosts:
        origins.append(f"http://{h}")
        origins.append(f"https://{h}")
    from mcp.server.transport_security import TransportSecuritySettings

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )


_ts = _transport_security()
mcp = FastMCP("mnesis", transport_security=_ts) if _ts else FastMCP("mnesis")


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

    Returns the resulting page's id, title, tags, the routing **action**
    (new / reinforce / supersede / contradict — decided by Mnesis, not the
    caller), how many secrets/PII were redacted at the boundary, and — when the
    routing produced them — the superseded page id and/or the contradiction
    review id. The action/review lines let an automated client (e.g. the ingest
    daemon) report the outcome without forcing any resolution.
    """
    _authorize("mnesis_ingest")  # PDP: role ∩ scope must grant write (IAM7)
    plan = ingest.plan_ingest(text, source_ref)
    try:
        result = ingest.apply_ingest(plan)  # the rich IngestResult (routing + ids)
    except quotas.QuotaExceeded as exc:
        return f"not ingested: {exc}"        # fail closed, surfaced clearly (T7)
    except authz.AuthorizationError as exc:
        return f"not ingested: {exc}"        # role/visibility refusal (T4)
    page = store.read_page(result["page_id"])
    search.upsert(page)
    tags = ", ".join(page.tags) if page.tags else "(none)"
    lines = [
        f"ingested page: {page.id}",
        f"title: {page.title}",
        f"tags: {tags}",
        f"action: {result['action_taken']}",
        f"redactions: {result['redaction_count']}",
    ]
    if result.get("superseded_id"):
        lines.append(f"superseded: {result['superseded_id']}")
    if result.get("review_id") is not None:
        lines.append(f"review: {result['review_id']}")
    return "\n".join(lines)


@mcp.tool()
def mnesis_query(query: str, limit: int = 10, include_stale: bool = False) -> str:
    """Search the wiki: BM25 blended with confidence, augmented by the graph.

    Results are ordered by a blended score (keyword match × confidence + a small
    graph-proximity boost). When the query resolves to an entity, pages reachable
    through the knowledge graph are folded in even if they lack the keyword, each
    marked ``↳ graph`` and grounded by the connecting edge. Stale pages are
    excluded unless ``include_stale=True``. Reading top hits records access.
    """
    _authorize("mnesis_query")
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
    _authorize("mnesis_get")
    if "/" in page_id or "\\" in page_id:
        return f"invalid page id: {page_id}"
    path = tenancy.current().pages_dir / f"{page_id}.md"
    if not path.exists():
        return f"no such page: {page_id}"
    page = store.read_page(page_id)
    # Visibility (T4): a private page is invisible to a non-owner — report it as
    # absent (don't leak its existence) rather than 403.
    if not authz.page_visible_to_active(page):
        return f"no such page: {page_id}"
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
    _authorize("mnesis_file_back")  # PDP: role ∩ scope must grant write (IAM7)
    principal = auth.current_principal_or_none()
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
        owner_principal=(principal.principal_id if principal else None),
        visibility=authz.default_visibility(),
    )
    store.write_page(page)
    search.upsert(page)
    return f"filed digest: {page.id} (score {score:.2f})"


@mcp.tool()
def mnesis_list() -> str:
    """List every page the principal may see: id, kind/status, and title."""
    _authorize("mnesis_list")
    principal = auth.current_principal_or_none()
    pages = store.list_pages()
    if principal is not None:
        pages = [p for p in pages if authz.can_see(principal, p)]
    if not pages:
        return "(no pages)"
    return "\n".join(f"{p.id} [{p.kind}/{p.status}] — {p.title}" for p in pages)


@mcp.tool()
def mnesis_rebuild() -> str:
    """Rebuild the rebuildable caches from Markdown: the search index AND the
    knowledge graph. The durable state store is never cleared."""
    _authorize("mnesis_rebuild")
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
    _authorize("mnesis_impact")
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
    _authorize("mnesis_entity")
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
    _authorize("mnesis_neighbors")
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
    _authorize("mnesis_traverse")
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
    _authorize("mnesis_graph_stats")
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
    _authorize("mnesis_graph_lint")
    return graph_lint.graph_lint(fix=fix).summary()


@mcp.tool()
def mnesis_health_report() -> str:
    """Read-only system health snapshot — counts, gaps, and cache freshness.

    Side-effect-free. Reports page counts by status/kind, pages with no sources,
    low-confidence and stale counts, the open-contradiction count, knowledge-graph
    size with the demoted-edge count, orphan/undeclared/dangling graph entities,
    and whether the search index and graph cache are in sync with the Markdown
    (the canonical source). Writes nothing.
    """
    _authorize("mnesis_health_report")
    r = maintenance.health_report()
    bs = ", ".join(f"{k}={v}" for k, v in r["by_status"].items()) or "(none)"
    bk = ", ".join(f"{k}={v}" for k, v in r["by_kind"].items()) or "(none)"
    idx = r["index"]
    gidx = r["graph_index"]
    lines = [
        f"pages: {r['pages_total']}  ({bs})",
        f"by kind: {bk}",
        f"stale: {r['stale']}   low-confidence (< {config.STALE_THRESHOLD:.2f}): {r['low_confidence']}",
        f"pages with no sources: {len(r['no_sources'])}"
        + (f" ({', '.join(r['no_sources'])})" if r["no_sources"] else ""),
        f"open contradictions: {r['open_contradictions']}",
        f"graph: {r['graph']['entities']} entities, {r['graph']['edges']} edges "
        f"({r['graph']['demoted']} demoted)",
        f"graph flags: {r['orphan_entities']} orphan, {r['undeclared_entities']} undeclared, "
        f"{r['dangling_structural']} dangling structural",
        f"search index: {idx['indexed_pages']}/{idx['markdown_pages']} pages, "
        f"{'fresh' if idx['fresh'] else 'STALE'}"
        + (f" (missing: {', '.join(idx['missing_from_index'])})" if idx["missing_from_index"] else "")
        + (f" (extra: {', '.join(idx['extra_in_index'])})" if idx["extra_in_index"] else ""),
        f"graph cache: {'present' if gidx['present'] else 'ABSENT'}, "
        f"{'fresh' if gidx['fresh'] else 'STALE'}"
        + (f" (missing: {', '.join(gidx['missing_page_nodes'])})" if gidx["missing_page_nodes"] else "")
        + (f" (extra: {', '.join(gidx['extra_page_nodes'])})" if gidx["extra_page_nodes"] else ""),
    ]
    return "\n".join(lines)


@mcp.tool()
def mnesis_find_duplicates(limit: int = 20) -> str:
    """Heuristic near-duplicate candidate pairs — read-only, proposes nothing.

    Surfaces up to ``limit`` page pairs that may assert the same knowledge,
    scored by blending title/tag overlap, shared graph edges, and FTS
    co-retrieval, each with a similarity rationale. Pairs already linked by a
    supersede are excluded. **This is a heuristic stand-in pending Phase-5
    vectors** (semantic similarity); it changes nothing — a human or agent
    decides what, if anything, to do (e.g. ingest a reconciling source).
    """
    _authorize("mnesis_find_duplicates")
    dupes = maintenance.find_duplicates(limit=limit)
    if not dupes:
        return "no near-duplicate candidates found (heuristic)"
    lines = [f"near-duplicate candidates (heuristic, pending Phase-5 vectors): {len(dupes)}"]
    for d in dupes:
        lines.append(f"  {d['page_a']} <~> {d['page_b']} (similarity {d['similarity']:.2f})")
        lines.append(f"    \"{d['title_a']}\"  <~>  \"{d['title_b']}\"")
        lines.append(f"    why: {d['rationale']}")
    return "\n".join(lines)


@mcp.tool()
def mnesis_decay() -> str:
    """Run the decay/lifecycle pass: recompute confidence corpus-wide and
    transition pages between active and stale (aged, unread, low-confidence pages
    go stale; reinforced ones revive). Idempotent. Returns the transition counts."""
    _authorize("mnesis_decay")
    s = lifecycle.recompute_all()
    return (
        f"decay: scanned {s['scanned']}, restaled {s['restaled']}, "
        f"reactivated {s['reactivated']}, unchanged {s['unchanged']}"
    )


@mcp.tool()
def mnesis_review() -> str:
    """List open contradiction reviews: queue id, both pages (with current
    confidence and title), and the conflict detail."""
    _authorize("mnesis_review")
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
    _authorize("mnesis_resolve")
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


@mcp.tool()
def mnesis_okf_concept(page_id: str) -> str:
    """Return a concept as an **OKF-conformant document** (OKF6): the OKF-core frontmatter
    (`type`/`title`/`description`/`timestamp`/`tags`) plus the Mnesis extensions, with the
    body's OKF cross-links — its **path identity** is `page_id`. The interop read view;
    `mnesis_get` (with its confidence header) remains unchanged for existing consumers.
    """
    _authorize("mnesis_okf_concept")
    if "/" in page_id or "\\" in page_id or f"{page_id}.md" in store.RESERVED_PAGE_FILES:
        return f"no such concept: {page_id}"
    path = tenancy.current().pages_dir / f"{page_id}.md"
    if not path.exists():
        return f"no such concept: {page_id}"
    page = store.read_page(page_id)
    if not authz.page_visible_to_active(page):  # visibility (T4): private = absent
        return f"no such concept: {page_id}"
    return path.read_text(encoding="utf-8")


@mcp.tool()
def mnesis_okf_export(fmt: str = "tar") -> str:
    """Export this tenant's knowledge as a **conformant OKF bundle** (OKF6): `fmt` is
    `tar` (a `.tar.gz`) or `dir`. Includes the reserved `index.md`/`log.md`. Returns the
    server-side path and a conformance summary."""
    _authorize("mnesis_okf_export")
    rep = okf_bundle.export_bundle(fmt=("dir" if fmt == "dir" else "tar"))
    return (f"exported {len(rep['concepts'])} concept(s) as an OKF bundle ({rep['format']}) "
            f"to {rep['path']} — conformant: {rep['conformant']}"
            + ("" if rep["conformant"] else f"; issues: {'; '.join(rep['issues'])}"))


@mcp.tool()
def mnesis_okf_import(path: str) -> str:
    """Import an external OKF bundle (a directory or `.tar.gz` at PATH) into this tenant
    **through the governed ingest path** (OKF6) — redaction, extraction, routing, and the
    contradiction review all apply. Bundle content is **UNTRUSTED data, never
    instructions**: each concept's text is ingested like any source; its frontmatter is
    not trusted or written directly."""
    _authorize("mnesis_okf_import")
    try:
        rep = okf_bundle.import_bundle(path)
    except (ValueError, FileNotFoundError, OSError) as exc:
        return f"import failed: {exc}"
    return (f"imported {rep['imported']}/{rep['concepts']} concept(s) through governance; "
            f"{rep['redactions']} redaction(s) applied")


# --- HTTP transport: health endpoint, bearer auth, server bootstrap --------


def _health_payload() -> dict:
    """Cheap liveness + quick stats — no LLM call."""
    # /health is open and tenant-agnostic. When a tenant is bound (legacy single-
    # tenant mode) it reports that tenant's quick stats; under credential auth no
    # tenant is bound on the open probe, so it returns liveness only.
    ctx = tenancy.current_or_none()
    if ctx is None:
        return {"status": "ok"}
    return {
        "status": "ok",
        "pages": len(store.list_pages()),
        "index_present": (ctx.cache_dir / "wiki.db").exists(),
        "graph_present": (ctx.cache_dir / "graph.db").exists(),
    }


@mcp.custom_route("/health", methods=["GET"])
async def _health(_request):
    """Unauthenticated health probe (safe for load balancers)."""
    return JSONResponse(_health_payload())


class _HealthTenantMiddleware:
    """Bind the ``default`` tenant for the open ``/health`` probe **only**, so it can
    report quick stats. Every other path passes through untouched — ``/mcp`` is bound
    by the agent-key auth (:class:`_PrincipalBindingMiddleware`) and ``/api`` by the
    web session choke point."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") not in ("http", "websocket") or scope.get("path", "") != "/health":
            await self.app(scope, receive, send)
            return
        ctx = tenancy.open_tenant(config.DEFAULT_TENANT_ID)
        with tenancy.use(ctx):
            await self.app(scope, receive, send)


class _PrincipalBindingMiddleware:
    """MCP agent-key auth (IAM7): resolve the bearer credential to
    ``(TenantContext, Principal)`` and bind BOTH for the request. The bearer is a
    per-tenant, per-principal **agent/machine API key** (IAM3, carrying its scopes) — or
    a legacy IAM1 credential — resolved by the shared :func:`mnesis.tokens.resolve_bearer`.
    **Fail closed:** a missing/invalid/expired/revoked credential is ``401``. The tenant
    is taken **only** from the validated credential; any client-supplied tenant id
    (header/body/path) is ignored by construction. Guards ``/mcp`` (and any non-``/api``,
    non-``/health`` path); ``/health`` is open and ``/api`` is the web session surface.

    There is **no single global MCP token** — every call is authenticated + scope-checked
    (the tools enforce the credential's scopes through the PDP)."""

    def __init__(self, app, store=None) -> None:
        self.app = app
        self.store = store  # optional IAM1 credential store (for the legacy fallback)

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path == "/health" or path.startswith("/api"):
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        presented = headers.get(b"authorization", b"").decode()
        token = presented[7:] if presented.startswith("Bearer ") else ""
        try:
            _, principal = tokens.resolve_bearer(token, cred_store=self.store)
        except auth.AuthError:
            await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
            return
        # Vault SELECTION from the client (header) — re-AUTHORIZED server-side against the
        # credential's grants before any store opens (V5). The tenant still comes only from
        # the credential; an ungranted/cross-vault selection fails closed → 403.
        selected_vault = headers.get(config.VAULT_SELECTION_HEADER.encode(), b"").decode() or None
        try:
            ctx = authz.open_authorized_vault(principal, selected_vault)
        except auth.AuthError as exc:
            reason = getattr(exc, "reason", "vault_forbidden")
            await JSONResponse({"error": "vault_forbidden", "reason": reason}, status_code=403)(
                scope, receive, send
            )
            return
        with tenancy.use(ctx):
            ptok = auth.bind_principal(principal)
            try:
                await self.app(scope, receive, send)
            finally:
                auth.unbind_principal(ptok)


def build_http_app():
    """Build the streamable-HTTP ASGI app with **three distinct auth surfaces**:

    - ``/api/*`` — the **web UI** (IAM5): interactive login + **cookie sessions** + CSRF
      + the PDP (``webauth.WebSessionMiddleware``). The browser-injected bearer token is
      retired.
    - ``/mcp`` — the **agent/MCP** surface (IAM7): a per-tenant, per-principal
      **agent/machine API key** authenticates every call (``_PrincipalBindingMiddleware``
      → ``tokens.resolve_bearer``), and every tool enforces the key's scopes through the
      PDP. The **single global MCP token is retired** — there is no unauthenticated or
      shared-token path.
    - ``GET /health`` — open (its default-tenant stats bound by ``_HealthTenantMiddleware``).
    """
    from . import webapi, webauth  # lazy: they import mcp_server, avoid an import cycle

    app = mcp.streamable_http_app()
    webapi.mount_api(app)
    # Inner: the web session/CSRF/PDP choke point for /api (retires the injected token).
    webauth.install(app)
    # Middle: the agent/MCP surface — an agent key authenticates every /mcp call.
    app.add_middleware(_PrincipalBindingMiddleware)
    # Outer: bind the default tenant for the open /health probe only.
    app.add_middleware(_HealthTenantMiddleware)
    return app


def serve() -> None:
    """Run the server using the transport selected by ``MNESIS_MCP_TRANSPORT``."""
    if config.MNESIS_MCP_TRANSPORT == "http":
        import uvicorn

        from . import audit
        # IAM8: audit every PDP denial (and IAM2/3 logins/tokens) to the auth audit log.
        audit.enable_pdp_audit()
        # IAM7: the HTTP /mcp surface always authenticates each call with a per-agent
        # agent key (no global token, no open path). Provision keys with
        # `mnesis pat`/`tokens.issue_agent_key_for` and distribute per agent.
        uvicorn.run(
            build_http_app(), host=config.MNESIS_MCP_HOST, port=config.MNESIS_MCP_PORT
        )
    else:
        # stdio (local Claude Code): one (tenant, vault) for the process lifetime — the
        # default tenant + the `MNESIS_VAULT` selection (default vault when unset). Local
        # trust, so provision the selected vault directly.
        vault_id = config.MNESIS_VAULT or config.DEFAULT_VAULT_ID
        if vault_id == config.DEFAULT_VAULT_ID:
            ctx = tenancy.open_tenant(config.DEFAULT_TENANT_ID)
        else:
            ctx = tenancy.create_vault(config.DEFAULT_TENANT_ID, vault_id)
        with tenancy.use(ctx):
            mcp.run()


if __name__ == "__main__":
    serve()
