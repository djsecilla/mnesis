"""Graph lint — consistency checks with safe self-healing (Phase 3).

The graph is built from extracted assertions, so it accumulates noise. This lint,
in the spirit of the Phase-1 self-healing principle, **auto-fixes what is safe**
and **flags the rest** for human review. All graph access is through the
``GraphBackend`` interface — no engine specifics here.

Auto-fixed (safe, deterministic):
  - **stale-only edges** — every supporting page is stale/superseded → demote
    (the cache marks them; they are already excluded by default).
  - **edge-confidence recompute** — refresh each edge's noisy-OR confidence from
    its pages' *current* Phase-2 confidence.
  - **duplicate edges** — same ``(s, p, o)`` in more than one row → merge
    provenance into one (defensive; ``finalize`` already dedups).

Flagged (never auto-changed):
  - **undeclared entities** — used in a relation but no page declares them as a tag.
  - **orphan entities** — declared as a tag but in no edge (informational).
  - **dangling structural edges** — ``supersedes``/``contradicts`` to a missing page.

Idempotent: a second ``fix`` run with no real change makes no mutations. Never
deletes an entity or an edge that still has any active supporting page.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from . import confidence, graph, state, store, vocab
from .graph import PAGE_NODE_TYPE

_CONF_TOLERANCE = 1e-6
_STRUCTURAL_PREDICATES = ("supersedes", "contradicts")


@dataclass
class LintReport:
    # Flagged for human review (never auto-changed).
    undeclared_entities: list[dict] = field(default_factory=list)
    orphan_entities: list[dict] = field(default_factory=list)
    dangling_structural: list[dict] = field(default_factory=list)
    # Auto-fixable categories (listed = actionable this run).
    duplicate_edges: list[dict] = field(default_factory=list)
    stale_only_edges: list[dict] = field(default_factory=list)
    confidence_updates: list[dict] = field(default_factory=list)
    fixed: bool = False

    @property
    def changes(self) -> int:
        """Number of auto-fix actions taken (or available when not fixing)."""
        return len(self.duplicate_edges) + len(self.stale_only_edges) + len(self.confidence_updates)

    @property
    def flagged(self) -> int:
        return len(self.undeclared_entities) + len(self.orphan_entities) + len(self.dangling_structural)

    def summary(self) -> str:
        verb = "fixed" if self.fixed else "fixable"
        lines = [
            f"graph-lint: {self.changes} {verb}, {self.flagged} flagged for review",
        ]
        for ref_info in self.undeclared_entities:
            lines.append(
                f"  [flag] undeclared entity {ref_info['ref']} "
                f"(used by: {', '.join(ref_info['suggested_pages'])} — add it as a tag)"
            )
        for o in self.orphan_entities:
            lines.append(f"  [flag] orphan entity {o['ref']} ({o['type']}) — declared but in no edge")
        for d in self.dangling_structural:
            s, p, ob = d["edge"]
            lines.append(f"  [flag] dangling {p}: {s} -> {ob} (missing {d['missing']})")
        for d in self.duplicate_edges:
            lines.append(f"  [{verb}] duplicate edge {d['triple']} merged")
        for e in self.stale_only_edges:
            lines.append(f"  [{verb}] demoted stale-only edge {e['triple']}")
        for c in self.confidence_updates:
            lines.append(
                f"  [{verb}] confidence {c['triple']}: {c['old']:.3f} -> {c['new']:.3f}"
            )
        if self.changes == 0 and self.flagged == 0:
            lines.append("  clean — nothing to fix or flag")
        return "\n".join(lines)


def _noisy_or(confs: list[float]) -> float:
    return 1.0 - math.prod(1.0 - c for c in confs) if confs else 0.0


def graph_lint(fix: bool = False, now: datetime | None = None) -> LintReport:
    """Lint the graph cache; with ``fix=True``, apply the safe auto-fixes.

    ``now`` is injectable so confidence recompute is deterministic (and a repeat
    ``fix`` run is a true no-op) in tests.
    """
    backend = graph.get_graph_backend()
    pages = {p.id: p for p in store.list_pages()}
    report = LintReport(fixed=fix)

    def page_conf(pid: str) -> float:
        return confidence.compute_confidence(pages[pid], access=state.get_access(pid), now=now)[0]

    def page_active(pid: str) -> bool:
        p = pages[pid]
        return p.status == "active" and p.superseded_by is None

    # Entity refs a page declares (entity-typed tags).
    declared: set[str] = set()
    for p in pages.values():
        for tag in p.tags:
            try:
                declared.add(vocab.normalize_ref(tag))
            except ValueError:
                continue

    edges = backend.all_edges()

    # --- duplicate edges: merge provenance into one row (defensive) ---
    by_triple: dict[tuple, list[dict]] = defaultdict(list)
    for e in edges:
        by_triple[(e["s"], e["p"], e["o"])].append(e)
    for triple, grp in by_triple.items():
        if len(grp) > 1:
            report.duplicate_edges.append({"triple": triple, "ids": [g["id"] for g in grp]})
            if fix:
                merged = sorted({pid for g in grp for pid in g["source_pages"]})
                backend.update_edge(grp[0]["id"], source_pages=merged, assertion_count=len(merged))
                for g in grp[1:]:
                    backend.delete_edge(g["id"])
    if fix and report.duplicate_edges:
        edges = backend.all_edges()  # refresh after merges

    # --- per-edge: stale-only demote + confidence recompute (auto-fix) ---
    for e in edges:
        triple = (e["s"], e["p"], e["o"])
        live = [pid for pid in e["source_pages"] if pid in pages]
        new_conf = _noisy_or([page_conf(pid) for pid in live])
        stale_only = not any(page_active(pid) for pid in live)

        if stale_only and not e["demoted"]:
            report.stale_only_edges.append({"triple": triple, "source_pages": e["source_pages"]})
        if abs(new_conf - e["confidence"]) > _CONF_TOLERANCE:
            report.confidence_updates.append({"triple": triple, "old": e["confidence"], "new": new_conf})

        if fix:
            updates: dict = {}
            if abs(new_conf - e["confidence"]) > _CONF_TOLERANCE:
                updates["confidence"] = new_conf
            if stale_only and not e["demoted"]:
                updates["demoted"] = True
            if updates:
                backend.update_edge(e["id"], **updates)

    # --- flag-only categories ---
    edge_entity_refs: set[str] = set()
    for e in edges:
        for ref in (e["s"], e["o"]):
            if not ref.startswith("page:"):
                edge_entity_refs.add(ref)

    # Undeclared: used in an edge but no page tags it.
    for ref in sorted(edge_entity_refs):
        if ref not in declared:
            suggesters = sorted(
                {pid for e in edges if ref in (e["s"], e["o"]) for pid in e["source_pages"]}
            )
            report.undeclared_entities.append({"ref": ref, "suggested_pages": suggesters})

    # Orphan: a declared (tag) entity that participates in no edge.
    for ent in backend.all_entities():
        if ent["type"] == PAGE_NODE_TYPE:
            continue
        if ent["ref"] not in edge_entity_refs:
            report.orphan_entities.append({"ref": ent["ref"], "type": ent["type"]})

    # Dangling structural edges: supersedes/contradicts to a missing page.
    seen_dangling: set = set()
    for e in edges:
        if e["p"] not in _STRUCTURAL_PREDICATES:
            continue
        for ref in (e["s"], e["o"]):
            if ref.startswith("page:") and ref[len("page:"):] not in pages:
                key = ((e["s"], e["p"], e["o"]), ref)
                if key not in seen_dangling:
                    seen_dangling.add(key)
                    report.dangling_structural.append(
                        {"edge": (e["s"], e["p"], e["o"]), "missing": ref}
                    )

    return report
