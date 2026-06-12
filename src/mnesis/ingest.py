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

**Plan / apply split (G7).** The pipeline is exposed as two steps so a UI can
preview before committing:

  * ``plan_ingest(raw_text, source_ref) -> IngestPlan`` runs scrub + extract +
    classify and returns a plain, serializable dict. It performs **zero writes
    and zero commits** — not even persisting the source.
  * ``apply_ingest(plan, overrides=None) -> IngestResult`` honours overrides
    (edited title/tags, rejected relations, a forced routing) and performs the
    writes (persist source, routed page write, commit, reindex).

``ingest_source`` is just ``plan_ingest`` followed by ``apply_ingest``, so CLI
and MCP behaviour is unchanged.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass

from . import config, confidence, llm, search, state, store, vocab
from .filters import scrub
from .store import Page

log = logging.getLogger(__name__)

# --- Extraction prompt ------------------------------------------------------

EXTRACTION_SYSTEM_PROMPT = """You extract a single, well-formed knowledge-base \
page from a source document. The source has reference id: {source_ref}.

Return ONLY a JSON object (no prose, no code fences) with exactly these keys:
  - "title": a one-line DECLARATIVE statement of the claim the source makes
    (e.g. "Project Atlas uses Redis for caching"), not a topic label.
  - "summary_markdown": clean Markdown prose stating only what the source
    supports. Mark any uncertainty explicitly. Do not invent facts, names,
    numbers, or relationships.
  - "key_facts": a list of short, discrete factual strings drawn from the source.
  - "tags": lowercase "type:value" entity refs for every entity the source
    mentions, using ONLY the entity types {entity_types} (e.g. "project:atlas",
    "library:redis", "person:sarah"). Reuse existing forms where possible.
  - "relations": a list of {{"s","p","o"}} triples that the source explicitly
    supports, where "s" and "o" are entity refs (as in tags) and "p" is one of
    the allowed predicates {predicates}. State the direction as "A -p-> B".

Discipline: cite only the given source; state nothing the source does not
support. Do not invent entities or relationships. Prefer FEWER, well-grounded
edges over speculative ones, and prefer one coherent claim per page."""

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
    system = EXTRACTION_SYSTEM_PROMPT.format(
        source_ref=source_ref,
        entity_types=", ".join(vocab.ENTITY_TYPES),
        predicates=", ".join(vocab.PREDICATES),
    )
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


def _normalize_tags(raw_tags) -> list[str]:
    """Normalize extracted tags, deduped and order-preserving.

    A ``type:value`` tag whose type is a known entity type is canonicalized via
    ``vocab.normalize_ref``; anything else is kept as a lowercased free tag.
    """
    out: list[str] = []
    for raw in raw_tags or []:
        tag = str(raw).strip()
        if not tag:
            continue
        try:
            tag = vocab.normalize_ref(tag)
        except ValueError:
            tag = tag.lower()  # not an entity ref — keep as a free tag
        if tag not in out:
            out.append(tag)
    return out


def _validate_relations(raw_relations) -> tuple[list[dict], list[dict]]:
    """Validate/normalize extracted triples. Returns ``(valid, dropped)``.

    ``valid`` is deduplicated, normalized triples; ``dropped`` records each
    rejected triple with a human-readable reason (never written, only reported).
    """
    valid: list[dict] = []
    dropped: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in raw_relations or []:
        try:
            rel = vocab.validate_relation(raw)
        except ValueError as exc:
            dropped.append({"triple": raw, "reason": str(exc)})
            continue
        key = (rel["s"], rel["p"], rel["o"])
        if key not in seen:
            seen.add(key)
            valid.append(rel)
    return valid, dropped


def _extract_draft(redacted: str, source_ref: str) -> tuple[dict, list[str]]:
    """Extract the side-effect-free **draft** page from a redacted source.

    Returns ``(draft, warnings)`` where ``draft`` has the serializable keys
    ``{title, summary_markdown, body, tags, relations, kind}`` — entities are
    normalized refs and relations are validated triples (invalid ones dropped and
    logged, never written, CLAUDE.md §7). No page is created and nothing written.
    """
    warnings: list[str] = []
    data = _extract(redacted, source_ref)
    if data is None:
        first_line = next((ln.strip() for ln in redacted.splitlines() if ln.strip()), source_ref)
        title = first_line[:80] or source_ref
        body = f"{redacted.strip()}\n\nSource: {source_ref}. (Extraction fell back to raw source.)"
        warnings.append("extraction fell back to a minimal page (LLM output was not parseable)")
        return {"title": title, "summary_markdown": "", "body": body,
                "tags": [], "relations": [], "kind": "fact"}, warnings

    title = data["title"].strip()
    summary = (data.get("summary_markdown") or "").strip()
    body = _build_body(data, source_ref)
    tags = _normalize_tags(data.get("tags"))
    relations, dropped = _validate_relations(data.get("relations"))
    for d in dropped:
        log.warning("ingest %s: dropped invalid relation %r — %s",
                    source_ref, d["triple"], d["reason"])
        warnings.append(f"dropped invalid relation {d['triple']!r}: {d['reason']}")
    # Every entity that an edge touches should appear as a tag.
    for rel in relations:
        for ref in (rel["s"], rel["o"]):
            if ref not in tags:
                tags.append(ref)

    return {"title": title, "summary_markdown": summary, "body": body,
            "tags": tags, "relations": relations, "kind": "fact"}, warnings


def _draft_to_page(draft: dict, source_ref: str) -> Page:
    """Build the candidate ``Page`` (not yet written) from a draft dict + ref."""
    return Page(
        id=store.make_id(draft["title"]),
        title=draft["title"],
        body=draft["body"],
        sources=[source_ref],
        tags=list(draft.get("tags") or []),
        relations=[dict(r) for r in (draft.get("relations") or [])],
        kind=draft.get("kind", "fact"),
    )


# --- Redaction reporting (no values, ever) ---------------------------------


def _aggregate_redactions(findings: list[dict]) -> list[dict]:
    """Aggregate scrub findings into ``[{type, kind, count}]`` — never any value."""
    counts = Counter((f["type"], f["kind"]) for f in findings)
    return [{"type": t, "kind": k, "count": n} for (t, k), n in sorted(counts.items())]


def _secret_warnings(findings: list[dict]) -> list[str]:
    """One warning per distinct secret kind detected (the value is never named)."""
    kinds = sorted({f["kind"] for f in findings if f.get("type") == "secret"})
    return [f"secret material was detected and redacted before extraction: {k}" for k in kinds]


# --- Classification --------------------------------------------------------


def _find_candidates(new_page: Page) -> list[Page]:
    """Top-N active existing pages that might relate to ``new_page``.

    Matches on the title — the declarative claim is the stable signal for
    reinforce/supersede/contradict. (Entity tags are not part of the query: with
    FTS5's implicit-AND, a new page's extra entity tags would exclude an existing
    page that doesn't share them. The LLM classifier makes the final call.)
    """
    query = new_page.title
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


@dataclass
class _Outcome:
    """The result of a routed write: the resulting page + what happened to it."""

    page: Page
    action: str  # new | reinforce | supersede | contradict
    superseded_id: str | None = None
    review_id: int | None = None


def _confidence(page: Page) -> float:
    score, _ = confidence.compute_confidence(page, access=state.get_access(page.id))
    return score


def _create(new_page: Page) -> Page:
    store.write_page(new_page)
    search.upsert(new_page)
    return new_page


def _merge_relations(existing: list[dict], incoming: list[dict]) -> list[dict]:
    """Union two relation lists, deduped by (s, p, o), preserving order."""
    merged = list(existing)
    seen = {(r["s"], r["p"], r["o"]) for r in existing if {"s", "p", "o"} <= r.keys()}
    for rel in incoming:
        key = (rel["s"], rel["p"], rel["o"])
        if key not in seen:
            seen.add(key)
            merged.append(rel)
    return merged


def _reinforce(existing: Page, source_ref: str, new_page: Page) -> Page:
    """Same claim, new support: bump support, reset the retention clock, and union
    in any new valid entities/relations the new source contributes."""
    if source_ref not in existing.sources:
        existing.sources.append(source_ref)
    existing.source_count += 1
    existing.last_confirmed = store.now_iso()  # reinforcement resets retention
    existing.relations = _merge_relations(existing.relations, new_page.relations)
    for tag in new_page.tags:
        if tag not in existing.tags:
            existing.tags.append(tag)
    store.write_page(existing, message=f"mnesis: reinforce {existing.id}")
    search.upsert(existing)
    return existing


def _supersede(winner: Page, loser_id: str) -> Page:
    """Write ``winner`` and stale ``loser_id``, linking both ways."""
    store.supersede(loser_id, winner)  # writes winner (supersedes=loser), stales loser
    search.upsert(winner)
    search.upsert(store.read_page(loser_id))
    return winner


def _contradict(new_page: Page, old: Page) -> _Outcome:
    """Conflict with no textual winner: resolve by confidence margin, else coexist."""
    conf_new, conf_old = _confidence(new_page), _confidence(old)
    margin = config.AUTO_RESOLVE_MARGIN

    if conf_new - conf_old >= margin:
        _supersede(new_page, old.id)  # new clearly wins -> auto-resolved
        return _Outcome(new_page, "supersede", superseded_id=old.id)
    if conf_old - conf_new >= margin:
        # old clearly wins: write the new page, then stale it under the old.
        store.write_page(new_page)
        store.supersede(new_page.id, old)
        search.upsert(old)
        loser = store.read_page(new_page.id)
        search.upsert(loser)
        return _Outcome(loser, "supersede", superseded_id=new_page.id)

    # No clear winner: both coexist, cross-link contradicts, queue for review.
    new_page.contradicts.append(old.id)
    old.contradicts.append(new_page.id)
    store.write_page(new_page)
    store.write_page(old, message=f"mnesis: contradicts {old.id} <-> {new_page.id}")
    review_id = state.enqueue_contradiction(
        new_page.id, old.id, f"'{new_page.title}' conflicts with '{old.title}'"
    )
    search.upsert(new_page)
    search.upsert(old)
    return _Outcome(new_page, "contradict", review_id=review_id)


def _apply_action(action: str, new_page: Page, target: Page | None, source_ref: str) -> _Outcome:
    """Execute one routed write via the Phase-2 lifecycle helpers."""
    if action == "new":
        return _Outcome(_create(new_page), "new")
    if action == "reinforce":
        return _Outcome(_reinforce(target, source_ref, new_page), "reinforce")
    if action == "supersede":
        _supersede(new_page, target.id)
        return _Outcome(new_page, "supersede", superseded_id=target.id)
    if action == "contradict":
        return _contradict(new_page, target)
    raise ValueError(f"unknown routing action: {action!r}")


# --- Routing (the classification decision, no writes) ----------------------

# classifier label -> routing action verb
_LABEL_TO_ACTION = {"reinforces": "reinforce", "supersedes": "supersede",
                    "contradicts": "contradict"}


def _plan_routing(new_page: Page, redacted: str) -> dict:
    """Decide the lifecycle action against existing candidates — **no writes**.

    Mirrors the Phase-2 loop: classify candidates in search order and stop at the
    first non-``unrelated`` label. Records every candidate it evaluated (with the
    label and current confidence) for the preview.
    """
    candidates_info: list[dict] = []
    chosen_label: str | None = None
    chosen: Page | None = None
    for candidate in _find_candidates(new_page):
        label = _classify(new_page, candidate, redacted)
        candidates_info.append({
            "page_id": candidate.id,
            "title": candidate.title,
            "relation_label": label,
            "confidence": round(_confidence(candidate), 4),
        })
        if label in _LABEL_TO_ACTION:
            chosen_label, chosen = label, candidate
            break  # first relation wins (Phase-2 behaviour)

    routing: dict = {
        "action": "new",
        "target_page_id": None,
        "candidates": candidates_info,
        "auto_resolved": False,
        "margin": None,
    }
    if chosen is None:
        return routing
    routing["action"] = _LABEL_TO_ACTION[chosen_label]
    routing["target_page_id"] = chosen.id
    if chosen_label == "contradicts":
        # Preview whether the margin will auto-resolve into a supersede.
        diff = _confidence(new_page) - _confidence(chosen)
        routing["margin"] = round(diff, 4)
        routing["auto_resolved"] = abs(diff) >= config.AUTO_RESOLVE_MARGIN
    return routing


# --- Pipeline entry points -------------------------------------------------


def plan_ingest(raw_text: str, source_ref: str) -> dict:
    """Plan an ingest **without any writes** (scrub + extract + classify).

    Returns a plain, serializable ``IngestPlan`` dict (see module docstring). The
    redacted text is carried in ``redacted_text`` for ``apply_ingest``; the raw
    secret/PII values are never included anywhere. Performs zero commits — a
    previewed-then-abandoned source leaves nothing on disk.
    """
    redacted, findings = scrub(raw_text)
    draft, warnings = _extract_draft(redacted, source_ref)
    warnings = _secret_warnings(findings) + warnings
    new_page = _draft_to_page(draft, source_ref)
    routing = _plan_routing(new_page, redacted)
    return {
        "source_ref": source_ref,
        "redacted_text": redacted,
        "redactions": _aggregate_redactions(findings),
        "draft_page": draft,
        "routing": routing,
        "warnings": warnings,
    }


def _apply_overrides(draft: dict, overrides: dict) -> dict:
    """Return a copy of ``draft`` with edited title/tags and rejected relations
    applied. ``rejected_relations`` / ``accepted_relations`` are index lists into
    ``draft['relations']``."""
    out = dict(draft)
    if str(overrides.get("title", "")).strip():
        out["title"] = str(overrides["title"]).strip()
    if "tags" in overrides:
        out["tags"] = _normalize_tags(overrides["tags"])
    rels = list(draft.get("relations") or [])
    if "accepted_relations" in overrides:
        keep = set(overrides["accepted_relations"] or [])
        rels = [r for i, r in enumerate(rels) if i in keep]
    elif overrides.get("rejected_relations"):
        drop = set(overrides["rejected_relations"])
        rels = [r for i, r in enumerate(rels) if i not in drop]
    out["relations"] = rels
    return out


def apply_ingest(plan: dict, overrides: dict | None = None) -> dict:
    """Apply a plan: persist the source and perform the routed write.

    ``overrides`` (optional) may carry an edited ``title``/``tags``, dropped
    relations (``rejected_relations``/``accepted_relations`` index lists), and a
    forced ``routing`` ``{action, target_page_id}``. A forced non-``new`` target
    must exist. Returns an ``IngestResult`` dict.
    """
    overrides = overrides or {}
    source_ref = plan["source_ref"]
    redacted = plan["redacted_text"]

    draft = _apply_overrides(plan["draft_page"], overrides)
    new_page = _draft_to_page(draft, source_ref)

    # Effective routing: a forced override wins over the planned decision.
    forced = overrides.get("routing")
    if forced:
        action = forced.get("action")
        target_id = forced.get("target_page_id")
        if action not in ("new", "reinforce", "supersede", "contradict"):
            raise ValueError(f"invalid forced routing action: {action!r}")
        if action != "new" and not (target_id and store.page_exists(target_id)):
            raise ValueError(f"forced {action} target does not exist: {target_id!r}")
    else:
        action = (plan.get("routing") or {}).get("action", "new")
        target_id = (plan.get("routing") or {}).get("target_page_id")

    # Persist the redacted source for provenance (committed by the store).
    store.write_source(source_ref, redacted)

    target = store.read_page(target_id) if (action != "new" and target_id) else None
    outcome = _apply_action(action, new_page, target, source_ref)

    return {
        "action_taken": outcome.action,
        "page_id": outcome.page.id,
        "superseded_id": outcome.superseded_id,
        "review_id": outcome.review_id,
        "redaction_count": sum(r["count"] for r in plan.get("redactions") or []),
    }


def ingest_source(raw_text: str, source_ref: str) -> Page:
    """Run the full relation-aware pipeline for one source (plan then apply).

    Returns the resulting page (the new page, or the existing page in the
    reinforce case) — unchanged from the prior one-shot behaviour."""
    plan = plan_ingest(raw_text, source_ref)
    result = apply_ingest(plan)
    return store.read_page(result["page_id"])
