"""The ingestion pipeline: raw source -> the right lifecycle action.

Pipeline contract (CLAUDE.md §7), strictly in order:

  1. **Scrub first** — redact secrets/PII; everything downstream uses the
     redacted text only. Nothing raw is persisted or sent to the LLM.
  2. **Persist the source** — save the redacted source for provenance.
  3. **Extract** — call the LLM for structured JSON, parsing robustly.
  4. **Classify & route** (Phase 2) — find candidate existing pages via search,
     classify the new info against each, and route to the lifecycle action:
       * ``reinforces`` -> bump support on the existing page (no new page),
       * ``supersedes`` -> write the new page and stale the old (links both ways),
       * ``contradicts`` -> auto-resolve by confidence margin, else coexist +
         cross-link + queue for review,
       * ``unrelated`` -> create a new page (Phase-1 behaviour).

Confidence is consulted to auto-resolve clear-margin contradictions. A page is
never silently deleted — losers go ``stale`` via supersede, or are queued.
"""

from __future__ import annotations

import json
import re

from . import config, confidence, llm, search, state, store
from .filters import scrub
from .store import Page

# --- Extraction prompt (unchanged contract) --------------------------------

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

# --- Classifier prompt (Phase 2) -------------------------------------------
# Conservative: must justify, defaults to "unrelated" when unsure. It names all
# four labels, which the offline stub keys on to recognize a classification call.

CLASSIFIER_SYSTEM_PROMPT = """You classify how NEW information relates to an \
EXISTING knowledge-base page, so the wiki can reinforce, supersede, flag, or \
branch knowledge instead of blindly duplicating it.

Choose exactly one label:
  - "reinforces": the new info asserts the SAME claim as the existing page,
    adding independent support. No facts change.
  - "supersedes": the new info updates or replaces the existing claim (newer,
    more accurate, or a changed state of the world).
  - "contradicts": the new info directly conflicts with the existing claim and
    there is no clear winner from the text alone.
  - "unrelated": the new info is about something else, or you are not sure.

Be conservative: when the relationship is not clearly reinforces/supersedes/
contradicts, choose "unrelated". Return ONLY a JSON object:
  {"label": "<one of the four>", "justification": "<one sentence>"}"""


# --- Extraction ------------------------------------------------------------


def _strip_fences(raw: str) -> str:
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z0-9]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


def _widest_object(s: str) -> str | None:
    start, end = s.find("{"), s.rfind("}")
    return s[start : end + 1] if 0 <= start < end else None


def _parse_json_object(raw: str) -> dict | None:
    """Best-effort parse of a JSON object from model output (fences or widest {})."""
    candidate = _strip_fences(raw)
    for attempt in (candidate, _widest_object(candidate)):
        if attempt is None:
            continue
        try:
            data = json.loads(attempt)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _extract(redacted: str, source_ref: str) -> dict | None:
    """Extract the page dict, retrying once stricter; ``None`` if unparseable."""
    system = EXTRACTION_SYSTEM_PROMPT.format(source_ref=source_ref)
    for sys_prompt in (system, system + _STRICTER_SUFFIX):
        data = _parse_json_object(llm.complete(sys_prompt, redacted))
        if data and isinstance(data.get("title"), str) and data["title"].strip():
            return data
    return None


def _build_body(data: dict, source_ref: str) -> str:
    parts: list[str] = []
    summary = (data.get("summary_markdown") or "").strip()
    if summary:
        parts.append(summary)
    facts = [str(f).strip() for f in (data.get("key_facts") or []) if str(f).strip()]
    if facts:
        parts.append("\n".join(f"- {f}" for f in facts))
    parts.append(f"Source: {source_ref}.")
    return "\n\n".join(parts)


def _page_from_extraction(redacted: str, source_ref: str) -> Page:
    """Build the candidate new ``fact`` page (not yet written) from a source."""
    data = _extract(redacted, source_ref)
    if data is None:
        first_line = next((ln.strip() for ln in redacted.splitlines() if ln.strip()), source_ref)
        title = first_line[:80] or source_ref
        body = f"{redacted.strip()}\n\nSource: {source_ref}. (Extraction fell back to raw source.)"
        tags: list[str] = []
    else:
        title = data["title"].strip()
        body = _build_body(data, source_ref)
        tags = [str(t).strip() for t in (data.get("tags") or []) if str(t).strip()]
    return Page(id=store.make_id(title), title=title, body=body, sources=[source_ref],
                tags=tags, kind="fact")


# --- Classification --------------------------------------------------------


def _find_candidates(new_page: Page) -> list[Page]:
    """Top-N active existing pages that might relate to ``new_page``."""
    query = new_page.title + " " + " ".join(new_page.tags)
    candidates: list[Page] = []
    for hit in search.search(query, limit=config.CANDIDATE_TOP_N, include_stale=False):
        if hit.id == new_page.id:
            continue
        try:
            candidates.append(store.read_page(hit.id))
        except FileNotFoundError:
            continue
    return candidates


def _classify(new_page: Page, candidate: Page, redacted: str) -> str:
    """Classify new info vs an existing page; defaults to ``unrelated``."""
    user = (
        f"NEW INFORMATION:\nTitle: {new_page.title}\nBody: {new_page.body}\n"
        f"Raw source: {redacted}\n\n"
        f"EXISTING PAGE (id: {candidate.id}):\nTitle: {candidate.title}\n"
        f"Body: {candidate.body}"
    )
    data = _parse_json_object(llm.complete(CLASSIFIER_SYSTEM_PROMPT, user)) or {}
    label = data.get("label")
    return label if label in llm._RELATION_LABELS else "unrelated"


# --- Lifecycle actions -----------------------------------------------------


def _confidence(page: Page) -> float:
    score, _ = confidence.compute_confidence(page, access=state.get_access(page.id))
    return score


def _create(new_page: Page) -> Page:
    store.write_page(new_page)
    search.upsert(new_page)
    return new_page


def _reinforce(existing: Page, source_ref: str) -> Page:
    """Same claim, new support: bump support and reset the retention clock."""
    if source_ref not in existing.sources:
        existing.sources.append(source_ref)
    existing.source_count += 1
    existing.last_confirmed = store.now_iso()  # reinforcement resets retention
    store.write_page(existing, message=f"mnesis: reinforce {existing.id}")
    search.upsert(existing)
    return existing


def _supersede(winner: Page, loser_id: str) -> Page:
    """Write ``winner`` and stale ``loser_id``, linking both ways."""
    store.supersede(loser_id, winner)  # writes winner (supersedes=loser), stales loser
    search.upsert(winner)
    search.upsert(store.read_page(loser_id))
    return winner


def _contradict(new_page: Page, old: Page) -> Page:
    """Conflict with no textual winner: resolve by confidence margin, else coexist."""
    conf_new, conf_old = _confidence(new_page), _confidence(old)
    margin = config.AUTO_RESOLVE_MARGIN

    if conf_new - conf_old >= margin:
        return _supersede(new_page, old.id)  # new clearly wins
    if conf_old - conf_new >= margin:
        # old clearly wins: write the new page, then stale it under the old.
        store.write_page(new_page)
        store.supersede(new_page.id, old)
        search.upsert(old)
        loser = store.read_page(new_page.id)
        search.upsert(loser)
        return loser

    # No clear winner: both coexist, cross-link contradicts, queue for review.
    new_page.contradicts.append(old.id)
    old.contradicts.append(new_page.id)
    store.write_page(new_page)
    store.write_page(old, message=f"mnesis: contradicts {old.id} <-> {new_page.id}")
    state.enqueue_contradiction(
        new_page.id, old.id, f"'{new_page.title}' conflicts with '{old.title}'"
    )
    search.upsert(new_page)
    search.upsert(old)
    return new_page


# --- Pipeline entry point --------------------------------------------------


def ingest_source(raw_text: str, source_ref: str) -> Page:
    """Run the full relation-aware pipeline for one source. Returns the resulting
    page (the new page, or the existing page in the reinforce case)."""
    # 1. Scrub first — proceed with the redacted text only.
    redacted, _findings = scrub(raw_text)

    # 2. Persist the redacted source for provenance (committed by the store).
    store.write_source(source_ref, redacted)

    # 3. Extract the candidate page (not yet written).
    new_page = _page_from_extraction(redacted, source_ref)

    # 4. Classify against existing candidates and route to a lifecycle action.
    for candidate in _find_candidates(new_page):
        label = _classify(new_page, candidate, redacted)
        if label == "reinforces":
            return _reinforce(candidate, source_ref)
        if label == "supersedes":
            return _supersede(new_page, candidate.id)
        if label == "contradicts":
            return _contradict(new_page, candidate)
        # "unrelated" -> keep checking other candidates

    # No relation found -> create a fresh page (Phase-1 behaviour).
    return _create(new_page)
