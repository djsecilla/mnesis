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

#: The built-in default entity types (the ``type`` in a ``type:value`` ref),
#: used when ``MNESIS_ENTITY_TYPES`` is unset.
DEFAULT_ENTITY_TYPES: tuple[str, ...] = ("person", "project", "library", "concept", "file", "decision")

#: Reserved type names a custom set may NOT use: ``page`` labels the structural
#: page nodes the graph emits, so allowing it as an entity type would collide.
RESERVED_ENTITY_TYPES: frozenset[str] = frozenset({"page"})

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


def _snake_case(token: object) -> str:
    """snake_case a vocabulary token: lowercase, runs of non-alphanumerics -> ``_``.

    Makes matching forgiving — ``"Depends On"``, ``"depends-on"`` and
    ``"depends_on"`` all resolve to the same canonical ``depends_on`` — and
    canonicalises user-supplied custom predicates and entity types the same way.
    """
    return re.sub(r"[^a-z0-9]+", "_", str(token).strip().lower()).strip("_")


def _resolve_predicates() -> tuple[str, ...]:
    """The active predicate set: ``MNESIS_PREDICATES`` (if set) else the default,
    normalised, de-duplicated, and always including :data:`CORE_PREDICATES`."""
    raw = config.MNESIS_PREDICATES.strip()
    base = (
        tuple(_snake_case(p) for p in raw.split(",") if p.strip())
        if raw
        else DEFAULT_PREDICATES
    )
    out: list[str] = []
    for p in (*base, *CORE_PREDICATES):
        if p and p not in out:
            out.append(p)
    return tuple(out)


def _resolve_entity_types() -> tuple[str, ...]:
    """The active entity-type set: ``MNESIS_ENTITY_TYPES`` (if set) else the
    default, normalised, de-duplicated, with :data:`RESERVED_ENTITY_TYPES`
    dropped. Unlike predicates there is no forced core — the structural ``page``
    type is separate and never a member here."""
    raw = config.MNESIS_ENTITY_TYPES.strip()
    base = (
        tuple(_snake_case(t) for t in raw.split(",") if t.strip())
        if raw
        else DEFAULT_ENTITY_TYPES
    )
    out: list[str] = []
    for t in base:
        if t and t not in RESERVED_ENTITY_TYPES and t not in out:
            out.append(t)
    return tuple(out)


#: The directed predicates a relation may use (``A -p-> B``). Resolved from
#: ``MNESIS_PREDICATES`` at import time (default = :data:`DEFAULT_PREDICATES`),
#: always including :data:`CORE_PREDICATES`. See CLAUDE.md §6.
PREDICATES: tuple[str, ...] = _resolve_predicates()

#: The entity types an entity ref may carry. Resolved from ``MNESIS_ENTITY_TYPES``
#: at import time (default = :data:`DEFAULT_ENTITY_TYPES`). See CLAUDE.md §6.
ENTITY_TYPES: tuple[str, ...] = _resolve_entity_types()


def _resolve_symmetric() -> frozenset[str]:
    """Predicates whose direction is not meaningful (``A p B`` ⟺ ``B p A``).

    From ``MNESIS_SYMMETRIC_PREDICATES`` (default ``contradicts,related_to``),
    normalised and **intersected with the active predicate set** — a symmetric
    predicate that isn't a valid predicate is meaningless. Set the env to empty
    to disable symmetric handling entirely.
    """
    raw = config.MNESIS_SYMMETRIC_PREDICATES.strip()
    if not raw:
        return frozenset()
    active = set(PREDICATES)
    return frozenset(p for t in raw.split(",") if (p := _snake_case(t)) in active)


#: Predicates treated as undirected (see :func:`_resolve_symmetric`). A symmetric
#: edge is stored once (reciprocals collapse), traversed from either endpoint,
#: and drawn without a direction arrow.
SYMMETRIC_PREDICATES: frozenset[str] = _resolve_symmetric()


def is_symmetric(p: object) -> bool:
    """True if predicate ``p`` (normalised) is symmetric/undirected."""
    return isinstance(p, str) and _snake_case(p) in SYMMETRIC_PREDICATES


def canonical_edge(s: str, p: str, o: str) -> tuple[str, str, str]:
    """Canonical ``(s, p, o)`` for grouping. For a symmetric predicate the
    endpoints are order-normalised (``min``/``max``) so reciprocal assertions
    collapse onto one edge; directed predicates are returned unchanged."""
    if is_symmetric(p) and o < s:
        return (o, p, s)
    return (s, p, o)


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
    etype = _snake_case(raw_type)
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
    return isinstance(p, str) and _snake_case(p) in PREDICATES


def validate_relation(rel: object) -> dict:
    """Validate and normalize a ``{s, p, o}`` relation, or raise ``ValueError``.

    Returns a new dict with normalized refs and a lowercased predicate.
    """
    if not isinstance(rel, dict):
        raise ValueError(f"relation must be a mapping with keys s, p, o; got {type(rel).__name__}")
    missing = [k for k in _RELATION_KEYS if k not in rel]
    if missing:
        raise ValueError(f"relation {rel!r} is missing key(s): {', '.join(missing)}")

    predicate = _snake_case(rel["p"])
    if predicate not in PREDICATES:
        raise ValueError(
            f"unknown predicate {rel['p']!r}; must be one of {', '.join(PREDICATES)}"
        )
    return {
        "s": normalize_ref(str(rel["s"])),
        "p": predicate,
        "o": normalize_ref(str(rel["o"])),
    }
