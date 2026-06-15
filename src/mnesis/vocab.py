"""Entity & predicate vocabulary for the typed knowledge graph (Phase 3).

This module defines the controlled vocabulary that the `relations` frontmatter
field must conform to, and the pure validation/normalization helpers that keep
edges consistent (CLAUDE.md §6, "Domain vocabulary & graph contract"):

  - **Entities** are ``type:value`` refs — lowercase, hyphenated value.
  - **Relations** are ``{s, p, o}`` triples where ``s``/``o`` are entity refs and
    ``p`` is one of the allowed directed predicates.

No graph backend or extraction here — just the contract. These functions raise
``ValueError`` with a clear message on invalid input, and return a normalized
form on valid input, so callers can normalize-and-validate in one step.
"""

from __future__ import annotations

import re

from . import config

#: The entity types an entity ref may carry (the ``type`` in ``type:value``).
ENTITY_TYPES: tuple[str, ...] = ("person", "project", "library", "concept", "file", "decision")

#: Predicates the graph itself emits as structural page-level edges (supersession
#: / contradiction). These are ALWAYS part of the vocabulary, even under a custom
#: override, so the structural projection stays consistent.
CORE_PREDICATES: tuple[str, ...] = ("supersedes", "contradicts")

#: The built-in default predicate set, used when ``MNESIS_PREDICATES`` is unset.
#: Split into the original engineering relations and a general-purpose set added
#: so non-software knowledge (people, places, history, organisations) can also
#: form edges instead of leaving conceptually-connected entities stranded as
#: isolated nodes. ``related_to`` is the deliberate last-resort catch-all; the
#: extraction prompt instructs the model to prefer a more specific predicate.
#: NOTE: ``depends_on``/``uses`` drive ``graph.impact()`` — keep them in a custom
#: list if you rely on impact analysis.
DEFAULT_PREDICATES: tuple[str, ...] = (
    # engineering / project relations
    "uses",
    "depends_on",
    "owns",
    "caused",
    "fixed",
    "contradicts",
    "supersedes",
    # general-purpose relations
    "part_of",
    "located_in",
    "created",
    "precedes",
    "influences",
    "related_to",
)

_RELATION_KEYS = ("s", "p", "o")


def _normalize_predicate(p: object) -> str:
    """snake_case a predicate: lowercase, runs of non-alphanumerics -> ``_``.

    Makes matching forgiving — ``"Depends On"``, ``"depends-on"`` and
    ``"depends_on"`` all resolve to the same canonical ``depends_on`` — and
    canonicalises user-supplied custom predicates the same way.
    """
    return re.sub(r"[^a-z0-9]+", "_", str(p).strip().lower()).strip("_")


def _resolve_predicates() -> tuple[str, ...]:
    """The active predicate set: ``MNESIS_PREDICATES`` (if set) else the default,
    normalised, de-duplicated, and always including :data:`CORE_PREDICATES`."""
    raw = config.MNESIS_PREDICATES.strip()
    base = (
        tuple(_normalize_predicate(p) for p in raw.split(",") if p.strip())
        if raw
        else DEFAULT_PREDICATES
    )
    out: list[str] = []
    for p in (*base, *CORE_PREDICATES):
        if p and p not in out:
            out.append(p)
    return tuple(out)


#: The directed predicates a relation may use (``A -p-> B``). Resolved from
#: ``MNESIS_PREDICATES`` at import time (default = :data:`DEFAULT_PREDICATES`),
#: always including :data:`CORE_PREDICATES`. See CLAUDE.md §6.
PREDICATES: tuple[str, ...] = _resolve_predicates()


def normalize_ref(ref: str) -> str:
    """Normalize an entity ref to canonical ``type:value`` form, or raise.

    The type must be a known :data:`ENTITY_TYPES`; the value is lowercased and
    hyphenated (any run of non-alphanumeric characters becomes a single ``-``,
    with leading/trailing hyphens stripped). Deterministic: mixed-case/spaced
    inputs map to the same output.
    """
    if not isinstance(ref, str) or ":" not in ref:
        raise ValueError(f"entity ref must be 'type:value', got {ref!r}")
    raw_type, raw_value = ref.split(":", 1)
    etype = raw_type.strip().lower()
    if etype not in ENTITY_TYPES:
        raise ValueError(
            f"unknown entity type {raw_type.strip()!r} in ref {ref!r}; "
            f"must be one of {', '.join(ENTITY_TYPES)}"
        )
    value = re.sub(r"[^a-z0-9]+", "-", raw_value.strip().lower()).strip("-")
    if not value:
        raise ValueError(f"entity ref {ref!r} has an empty value")
    return f"{etype}:{value}"


def is_valid_predicate(p: object) -> bool:
    """True if ``p`` (normalised to snake_case) is an allowed predicate."""
    return isinstance(p, str) and _normalize_predicate(p) in PREDICATES


def validate_relation(rel: object) -> dict:
    """Validate and normalize a ``{s, p, o}`` relation, or raise ``ValueError``.

    Returns a new dict with normalized refs and a lowercased predicate.
    """
    if not isinstance(rel, dict):
        raise ValueError(f"relation must be a mapping with keys s, p, o; got {type(rel).__name__}")
    missing = [k for k in _RELATION_KEYS if k not in rel]
    if missing:
        raise ValueError(f"relation {rel!r} is missing key(s): {', '.join(missing)}")

    predicate = _normalize_predicate(rel["p"])
    if predicate not in PREDICATES:
        raise ValueError(
            f"unknown predicate {rel['p']!r}; must be one of {', '.join(PREDICATES)}"
        )
    return {
        "s": normalize_ref(str(rel["s"])),
        "p": predicate,
        "o": normalize_ref(str(rel["o"])),
    }
