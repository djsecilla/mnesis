"""FastMCP server exposing the mnesis wiki tools over stdio.

This is the agent-facing surface: Claude Code (and any MCP client) can ingest
sources, query the index, fetch pages, and file synthesized answers back. The
tools are thin orchestration over the core modules (filters, ingest, store,
search) — no business logic lives here that isn't in those modules.

Newly written pages are ``search.upsert``-ed into the index immediately, so a
``wiki_file_back`` answer (or a fresh ingest) surfaces on the next
``wiki_query`` — the compounding loop the PoC exists to demonstrate.

Verified against mcp 1.27.x: ``mcp.server.fastmcp.FastMCP``, ``@mcp.tool()``,
``mcp.run(transport="stdio")``.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from . import config, ingest, search, store
from .filters import scrub
from .store import Page

mcp = FastMCP("mnesis")


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
def wiki_ingest(text: str, source_ref: str) -> str:
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
def wiki_query(query: str, limit: int = 10) -> str:
    """Keyword-search the wiki (BM25). Returns ranked hits with snippets."""
    hits = search.search(query, limit)
    if not hits:
        return f'no results for "{query}"'
    lines = []
    for i, h in enumerate(hits, 1):
        lines.append(f"{i}. {h.id} — {h.title} (score {h.score:.3f})")
        lines.append(f"   {h.snippet}")
    return "\n".join(lines)


@mcp.tool()
def wiki_get(page_id: str) -> str:
    """Return the full Markdown (frontmatter + body) of a page by id."""
    if "/" in page_id or "\\" in page_id:
        return f"invalid page id: {page_id}"
    path = config.PAGES_DIR / f"{page_id}.md"
    if not path.exists():
        return f"no such page: {page_id}"
    return path.read_text(encoding="utf-8")


@mcp.tool()
def wiki_file_back(question: str, answer: str, quality_score: float | None = None) -> str:
    """File a synthesized answer back as a durable ``digest`` page (compounding).

    If ``quality_score`` (or the internal heuristic when ``None``) is at least
    ``WIKI_FILEBACK_THRESHOLD``, write a ``kind=digest`` page linking the
    question and answer and return its id. Otherwise file nothing and return the
    reason. Digest pages are tagged ``kind:digest`` so they never masquerade as
    primary sourced facts (CLAUDE.md §5, §9).
    """
    score = quality_score if quality_score is not None else _heuristic_quality(answer)
    threshold = config.WIKI_FILEBACK_THRESHOLD
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
def wiki_list() -> str:
    """List every page: id, kind/status, and title."""
    pages = store.list_pages()
    if not pages:
        return "(no pages)"
    return "\n".join(f"{p.id} [{p.kind}/{p.status}] — {p.title}" for p in pages)


@mcp.tool()
def wiki_rebuild() -> str:
    """Rebuild the search index from the Markdown pages (cache projection)."""
    n = search.rebuild()
    return f"rebuilt index from {n} page(s)"


if __name__ == "__main__":
    mcp.run(transport="stdio")
