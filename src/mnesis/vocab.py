"""Entity & predicate vocabulary for the typed knowledge graph (Phase 3; per-vault V3).

This module defines the controlled vocabulary that the `relations` frontmatter field
must conform to, and the pure validation/normalization helpers that keep edges consistent
(CLAUDE.md §6, "Domain vocabulary & graph contract"):

  - **Entities** are ``type:value`` refs — lowercase, hyphenated value.
  - **Relations** are ``{s, p, o}`` triples where ``s``/``o`` are entity refs and ``p``
    is one of the allowed directed predicates.

**Per-vault schema (V3).** The schema is a **per-vault** :class:`VaultConfig` (entity
types + predicates + symmetric set + knowledge-organization settings), stored under the
vault root. The module-level helpers (:func:`normalize_ref`, :func:`validate_relation`,
:func:`is_symmetric`, …) resolve the **active vault's** config (:func:`active_config`), so
the whole pipeline — extraction/classification and the typed-graph projection — validates
against the schema of the vault it is running in, with **no global schema authoritative**.
When no vault is bound (unit tests, pure helpers) they fall back to :func:`default_config`,
which equals the current global (env-resolved) schema — so existing behaviour is preserved.

**Unknown-type policy (tolerate-and-flag).** Consistent with OKF leniency: a tag that is
not a valid entity ref is **kept as a free tag** (not dropped — no data loss); a relation
whose predicate/entity type is unknown *for that vault* is **dropped from the typed graph
and flagged** (`ingest` returns it in `dropped`), keeping the typed graph clean while the
prose + free tags retain the information.

No graph backend or extraction here — just the contract. The helpers raise ``ValueError``
with a clear message on invalid input and return a normalized form on valid input.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

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

    Makes matching forgiving — ``"Depends On"``, ``"depends-on"`` and ``"depends_on"``
    all resolve to the same canonical ``depends_on`` — and canonicalises user-supplied
    custom predicates and entity types the same way.
    """
    return re.sub(r"[^a-z0-9]+", "_", str(token).strip().lower()).strip("_")


# --- Normalization of a vocabulary set (shared by the env resolvers + VaultConfig) ---


def normalize_predicate_set(items) -> tuple[str, ...]:
    """snake_case + de-dup a predicate list, always appending :data:`CORE_PREDICATES`."""
    out: list[str] = []
    for p in items:
        s = _snake_case(p)
        if s and s not in out:
            out.append(s)
    for c in CORE_PREDICATES:
        if c not in out:
            out.append(c)
    return tuple(out)


def normalize_entity_type_set(items) -> tuple[str, ...]:
    """snake_case + de-dup an entity-type list, dropping :data:`RESERVED_ENTITY_TYPES`."""
    out: list[str] = []
    for t in items:
        s = _snake_case(t)
        if s and s not in RESERVED_ENTITY_TYPES and s not in out:
            out.append(s)
    return tuple(out)


def normalize_symmetric_set(items, predicates) -> tuple[str, ...]:
    """snake_case a symmetric list, **intersected with** ``predicates`` (a symmetric
    predicate that isn't a valid predicate is meaningless)."""
    active = set(predicates)
    out: list[str] = []
    for t in items:
        s = _snake_case(t)
        if s in active and s not in out:
            out.append(s)
    return tuple(out)


# --- The env-resolved GLOBAL default schema (backward compat) -----------------


def _resolve_predicates() -> tuple[str, ...]:
    """The global predicate set: ``MNESIS_PREDICATES`` (if set) else the default."""
    raw = config.MNESIS_PREDICATES.strip()
    base = tuple(p for p in raw.split(",") if p.strip()) if raw else DEFAULT_PREDICATES
    return normalize_predicate_set(base)


def _resolve_entity_types() -> tuple[str, ...]:
    """The global entity-type set: ``MNESIS_ENTITY_TYPES`` (if set) else the default."""
    raw = config.MNESIS_ENTITY_TYPES.strip()
    base = tuple(t for t in raw.split(",") if t.strip()) if raw else DEFAULT_ENTITY_TYPES
    return normalize_entity_type_set(base)


#: The directed predicates a relation may use (``A -p-> B``). Resolved from
#: ``MNESIS_PREDICATES`` at import time; the GLOBAL DEFAULT — per-vault schemas
#: override it via :class:`VaultConfig`. See CLAUDE.md §6.
PREDICATES: tuple[str, ...] = _resolve_predicates()

#: The entity types an entity ref may carry (global default). See CLAUDE.md §6.
ENTITY_TYPES: tuple[str, ...] = _resolve_entity_types()


def _resolve_symmetric() -> frozenset[str]:
    """The global symmetric predicate set (``MNESIS_SYMMETRIC_PREDICATES``), intersected
    with the active predicate set. Empty env disables symmetric handling entirely."""
    raw = config.MNESIS_SYMMETRIC_PREDICATES.strip()
    if not raw:
        return frozenset()
    return frozenset(normalize_symmetric_set(raw.split(","), PREDICATES))


#: Predicates treated as undirected (global default). A symmetric edge is stored once
#: (reciprocals collapse), traversed from either endpoint, drawn without a direction arrow.
SYMMETRIC_PREDICATES: frozenset[str] = _resolve_symmetric()


# --- Pure normalization/validation against an explicit schema -----------------


def _normalize_ref_with(ref: str, entity_types) -> str:
    """Normalize an entity ref to canonical ``type:value`` against ``entity_types``, or
    raise. The type must be known; the value is lowercased and hyphenated."""
    if not isinstance(ref, str) or ":" not in ref:
        raise ValueError(f"entity ref must be 'type:value', got {ref!r}")
    raw_type, raw_value = ref.split(":", 1)
    etype = _snake_case(raw_type)
    if etype not in entity_types:
        raise ValueError(
            f"unknown entity type {raw_type.strip()!r} in ref {ref!r}; "
            f"must be one of {', '.join(entity_types)}"
        )
    value = re.sub(r"[^a-z0-9]+", "-", raw_value.strip().lower()).strip("-")
    if not value:
        raise ValueError(f"entity ref {ref!r} has an empty value")
    return f"{etype}:{value}"


def _validate_relation_with(rel: object, predicates, entity_types) -> dict:
    """Validate + normalize a ``{s, p, o}`` relation against an explicit schema, or raise."""
    if not isinstance(rel, dict):
        raise ValueError(f"relation must be a mapping with keys s, p, o; got {type(rel).__name__}")
    missing = [k for k in _RELATION_KEYS if k not in rel]
    if missing:
        raise ValueError(f"relation {rel!r} is missing key(s): {', '.join(missing)}")
    predicate = _snake_case(rel["p"])
    if predicate not in predicates:
        raise ValueError(f"unknown predicate {rel['p']!r}; must be one of {', '.join(predicates)}")
    return {
        "s": _normalize_ref_with(str(rel["s"]), entity_types),
        "p": predicate,
        "o": _normalize_ref_with(str(rel["o"]), entity_types),
    }


# --- The per-vault schema config (V3) ----------------------------------------


@dataclass(frozen=True)
class VaultConfig:
    """The knowledge-organization schema for ONE vault: the entity types + predicates the
    typed graph validates against, the symmetric (undirected) predicates, the default page
    visibility, plus a free-form ``settings`` extension point (e.g. per-vault decay
    classes). ``version`` supports forward migration of the config format.

    All vocabulary sets are normalized on construction (snake_cased, de-duped; predicates
    always include :data:`CORE_PREDICATES`; ``page`` dropped from entity types; the
    symmetric set intersected with the predicates)."""

    version: int = 1
    entity_types: tuple[str, ...] = DEFAULT_ENTITY_TYPES
    predicates: tuple[str, ...] = DEFAULT_PREDICATES
    symmetric_predicates: tuple[str, ...] = ("contradicts", "related_to")
    default_visibility: str = "shared"
    settings: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "entity_types", normalize_entity_type_set(self.entity_types))
        preds = normalize_predicate_set(self.predicates)
        object.__setattr__(self, "predicates", preds)
        object.__setattr__(self, "symmetric_predicates", normalize_symmetric_set(self.symmetric_predicates, preds))
        dv = str(self.default_visibility or "shared").strip().lower()
        object.__setattr__(self, "default_visibility", dv if dv in {"shared", "private"} else "shared")
        if not isinstance(self.settings, dict):
            object.__setattr__(self, "settings", {})

    # -- schema-scoped vocabulary operations (mirror the module helpers) --------
    def normalize_ref(self, ref: str) -> str:
        return _normalize_ref_with(ref, self.entity_types)

    def is_valid_predicate(self, p: object) -> bool:
        return isinstance(p, str) and _snake_case(p) in self.predicates

    def validate_relation(self, rel: object) -> dict:
        return _validate_relation_with(rel, self.predicates, self.entity_types)

    def is_symmetric(self, p: object) -> bool:
        return isinstance(p, str) and _snake_case(p) in self.symmetric_predicates

    def canonical_edge(self, s: str, p: str, o: str) -> tuple[str, str, str]:
        if self.is_symmetric(p) and o < s:
            return (o, p, s)
        return (s, p, o)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "entity_types": list(self.entity_types),
            "predicates": list(self.predicates),
            "symmetric_predicates": list(self.symmetric_predicates),
            "default_visibility": self.default_visibility,
            "settings": dict(self.settings),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "VaultConfig":
        d = d or {}
        return cls(
            version=int(d.get("version", 1) or 1),
            entity_types=tuple(d.get("entity_types") or DEFAULT_ENTITY_TYPES),
            predicates=tuple(d.get("predicates") or DEFAULT_PREDICATES),
            symmetric_predicates=tuple(
                d["symmetric_predicates"] if d.get("symmetric_predicates") is not None else ("contradicts", "related_to")
            ),
            default_visibility=d.get("default_visibility", "shared"),
            settings=dict(d.get("settings") or {}),
        )


def default_config() -> VaultConfig:
    """A :class:`VaultConfig` equal to the **current global (env-resolved) schema** — the
    default a new/migrated vault gets, and the fallback when no vault is bound. Reads the
    module globals *at call time* so a test that monkeypatches them still flows through."""
    return VaultConfig(
        entity_types=ENTITY_TYPES,
        predicates=PREDICATES,
        symmetric_predicates=tuple(sorted(SYMMETRIC_PREDICATES)),
        default_visibility=config.MNESIS_DEFAULT_VISIBILITY,
    )


# --- Load / save / resolve the active vault's config --------------------------

#: In-process cache: vault config path -> (mtime, VaultConfig). Keeps the hot graph/ingest
#: path from re-reading the JSON on every ref; invalidated on mtime change (an edit).
_config_cache: dict[str, tuple[float, VaultConfig]] = {}


def load_config(ctx) -> VaultConfig:
    """The :class:`VaultConfig` for ``ctx`` (a :class:`~mnesis.tenancy.VaultContext`).
    Reads ``<vault_root>/config.json`` (mtime-cached); a missing/unreadable file falls back
    to :func:`default_config` so a vault always has a working schema."""
    path = Path(ctx.config_path)
    key = str(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return default_config()
    cached = _config_cache.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        cfg = VaultConfig.from_dict(json.loads(path.read_text(encoding="utf-8") or "{}"))
    except (ValueError, OSError):
        cfg = default_config()
    _config_cache[key] = (mtime, cfg)
    return cfg


def save_config(ctx, cfg: VaultConfig) -> Path:
    """Persist ``cfg`` as ``<vault_root>/config.json`` (atomic). A vault admin/owner edits
    a vault's schema through this; changes affect only this vault."""
    path = Path(ctx.config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(cfg.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    _config_cache.pop(str(path), None)
    return path


def ensure_config(ctx) -> VaultConfig:
    """Idempotently ensure ``ctx`` has a config on disk, writing the DEFAULT schema (equal
    to the current global schema) if absent. Called at vault provisioning/migration."""
    path = Path(ctx.config_path)
    if not path.is_file():
        cfg = default_config()
        save_config(ctx, cfg)
        return cfg
    return load_config(ctx)


def active_config() -> VaultConfig:
    """The schema of the **currently bound vault** (fail-safe): the active
    :class:`~mnesis.tenancy.VaultContext`'s config, else :func:`default_config`. This is
    what every module-level helper resolves against, so the pipeline is per-vault with no
    global schema authoritative."""
    from . import tenancy  # lazy: avoid an import cycle (tenancy imports vocab)

    ctx = tenancy.current_or_none()
    if ctx is None or not isinstance(ctx, tenancy.VaultContext):
        return default_config()
    return load_config(ctx)


# --- Module-level helpers (resolve against the ACTIVE vault's schema) ----------


def is_symmetric(p: object) -> bool:
    """True if predicate ``p`` (normalised) is symmetric/undirected in the active vault."""
    return active_config().is_symmetric(p)


def canonical_edge(s: str, p: str, o: str) -> tuple[str, str, str]:
    """Canonical ``(s, p, o)`` for grouping in the active vault (symmetric endpoints are
    order-normalised so reciprocal assertions collapse; directed ones unchanged)."""
    return active_config().canonical_edge(s, p, o)


def normalize_ref(ref: str) -> str:
    """Normalize an entity ref to canonical ``type:value`` against the **active vault's**
    entity types, or raise ``ValueError``."""
    return active_config().normalize_ref(ref)


def is_valid_predicate(p: object) -> bool:
    """True if ``p`` is an allowed predicate in the active vault's schema."""
    return active_config().is_valid_predicate(p)


def validate_relation(rel: object) -> dict:
    """Validate and normalize a ``{s, p, o}`` relation against the active vault's schema,
    or raise ``ValueError``. Returns a new dict with normalized refs and predicate."""
    return active_config().validate_relation(rel)
