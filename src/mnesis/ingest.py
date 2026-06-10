"""The ingestion pipeline: raw source -> clean canonical page.

Pipeline contract (CLAUDE.md §7), strictly in order:

  1. **Scrub first** — redact secrets/PII; everything downstream uses the
     redacted text only. Nothing raw is persisted or sent to the LLM.
  2. **Persist the source** — save the redacted source for provenance.
  3. **Extract** — call the LLM with the disciplined prompt for structured JSON,
     parsing robustly (strip fences; retry once stricter; then fall back).
  4. **Write** — build a ``fact`` page and commit it via the store.

PoC simplification: every source creates a *new* fact page. Reinforcement /
contradiction / supersession detection is Phase 2 (CLAUDE.md §7).
"""

from __future__ import annotations

import json
import re

from . import llm, store
from .filters import scrub
from .store import Page

# The extraction contract lives in the system prompt. {source_ref} is the
# provenance handle the model must cite; it must never invent beyond the source.
EXTRACTION_SYSTEM_PROMPT = """You extract a single, well-formed knowledge-base \
page from a source document. The source has reference id: {source_ref}.

Return ONLY a JSON object (no prose, no code fences) with exactly these keys:
  - "title": a one-line DECLARATIVE statement of the claim the source makes
    (e.g. "Project Atlas uses Redis for caching"), not a topic label.
  - "summary_markdown": clean Markdown prose stating only what the source
    supports. Mark any uncertainty explicitly. Do not invent facts, names,
    numbers, or relationships.
  - "key_facts": a list of short, discrete factual strings drawn from the source.
  - "tags": a list of lowercase "type:value" tags using the entity types
    person/project/library/concept/file/decision plus free tags.

Discipline: cite only the given source; state nothing the source does not
support; prefer one coherent claim per page."""

_STRICTER_SUFFIX = (
    "\n\nIMPORTANT: Your previous output was not valid JSON. Respond with a "
    "SINGLE valid JSON object and nothing else — no commentary, no code fences."
)


def _strip_fences(raw: str) -> str:
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z0-9]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


def _parse_extraction(raw: str) -> dict:
    """Parse the model output into the extraction dict, robustly.

    Strips code fences; if that fails, falls back to the widest ``{...}`` slice.
    Raises ``ValueError`` if no valid JSON object with a ``title`` is found.
    """
    candidate = _strip_fences(raw)
    for attempt in (candidate, _widest_object(candidate)):
        if attempt is None:
            continue
        try:
            data = json.loads(attempt)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict) and isinstance(data.get("title"), str) and data["title"].strip():
            return data
    raise ValueError("no valid extraction JSON")


def _widest_object(s: str) -> str | None:
    start, end = s.find("{"), s.rfind("}")
    return s[start : end + 1] if 0 <= start < end else None


def _build_body(data: dict, source_ref: str) -> str:
    """Assemble clean Markdown body: summary, key facts, source citation."""
    parts: list[str] = []
    summary = (data.get("summary_markdown") or "").strip()
    if summary:
        parts.append(summary)
    facts = [str(f).strip() for f in (data.get("key_facts") or []) if str(f).strip()]
    if facts:
        parts.append("\n".join(f"- {f}" for f in facts))
    parts.append(f"Source: {source_ref}.")
    return "\n\n".join(parts)


def _fallback_page(redacted: str, source_ref: str, tags: list[str] | None = None) -> Page:
    """Minimal page built directly from the (redacted) source when extraction
    fails — never lets a source go un-ingested."""
    first_line = next((ln.strip() for ln in redacted.splitlines() if ln.strip()), source_ref)
    title = first_line[:80] or source_ref
    body = f"{redacted.strip()}\n\nSource: {source_ref}. (Extraction fell back to raw source.)"
    return Page(
        id=store.make_id(title),
        title=title,
        body=body,
        sources=[source_ref],
        tags=tags or [],
        kind="fact",
    )


def ingest_source(raw_text: str, source_ref: str) -> Page:
    """Run the full pipeline for one source and return the written ``fact`` page."""
    # 1. Scrub first — proceed with the redacted text only.
    redacted, _findings = scrub(raw_text)

    # 2. Persist the redacted source for provenance (committed by the store).
    store.write_source(source_ref, redacted)

    # 3. Extract structured JSON, robustly (retry once stricter, then fall back).
    system = EXTRACTION_SYSTEM_PROMPT.format(source_ref=source_ref)
    page: Page
    try:
        data = _parse_extraction(llm.complete(system, redacted))
    except ValueError:
        try:
            data = _parse_extraction(llm.complete(system + _STRICTER_SUFFIX, redacted))
        except ValueError:
            data = None

    if data is None:
        page = _fallback_page(redacted, source_ref)
    else:
        tags = [str(t).strip() for t in (data.get("tags") or []) if str(t).strip()]
        page = Page(
            id=store.make_id(data["title"].strip()),
            title=data["title"].strip(),
            body=_build_body(data, source_ref),
            sources=[source_ref],
            tags=tags,
            kind="fact",
        )

    # 4. Write the page (source_count=1, last_confirmed=now via Page defaults).
    store.write_page(page)
    return page
