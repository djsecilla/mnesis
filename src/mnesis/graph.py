"""The typed knowledge graph — a rebuildable projection of the Markdown.

The graph is a **pure cache** (CLAUDE.md §6/§8): it holds nothing that is not
derivable from the pages' `relations`/`type:value` tags plus their Phase-2
confidence. `rebuild_graph()` regenerates it; `mnesis rebuild` rebuilds the
search index and the graph together; neither clears the durable state store.

The engine is never load-bearing. All graph access goes through the
:class:`GraphBackend` interface — no SQL or engine types leak into ingest,
search, mcp_server, or cli. The default is :class:`SqliteGraphBackend` (embedded
SQLite at ``wiki/.index/graph.db``); a Tier-B backend (Postgres+Apache AGE,
Neo4j, or a graph-native server) implements the same interface and is selected
by :func:`get_graph_backend` via ``config.GRAPH_BACKEND`` — a config change, not
a refactor.

Edge model: a distinct ``(s, p, o)`` triple is one edge. Its ``source_pages`` are
the pages asserting it; ``assertion_count`` is how many; ``confidence`` is a
noisy-OR over those pages' confidence, ``1 - Π(1 - conf_i)``, so several weak
sources combine into a stronger edge. An edge supported only by stale/superseded
pages is **demoted** (excluded by default, never deleted).
"""

from __future__ import annotations

import json
import math
import sqlite3
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path

from . import config, confidence, state, store, vocab

# Page-level structural predicates projected from frontmatter (between page nodes).
PAGE_NODE_TYPE = "page"


def page_ref(page_id: str) -> str:
    """The entity ref for a page node (structural edges connect these)."""
    return f"page:{page_id}"


# --- Interface -------------------------------------------------------------


class GraphBackend(ABC):
    """The full graph surface the rest of the system uses.

    No engine-specific types appear in any signature; refs/predicates are plain
    strings, and results are plain dicts/lists. Build phase: ``clear`` ->
    ``add_entity``/``add_edge`` (many) -> ``finalize``. Query phase: ``get_entity``,
    ``neighbors``, ``traverse``.
    """

    # -- build --
    @abstractmethod
    def clear(self) -> None:
        """Drop all entities and edges (start a fresh projection)."""

    @abstractmethod
    def add_entity(self, ref: str, type: str) -> None:
        """Record an entity node (idempotent)."""

    @abstractmethod
    def add_edge(
        self, s: str, p: str, o: str, source_page: str, page_confidence: float,
        page_active: bool = True,
    ) -> None:
        """Record one assertion of edge ``s -p-> o`` by ``source_page``.

        ``page_active`` is False when the asserting page is stale/superseded; an
        edge with no active supporter is demoted in :meth:`finalize`.
        """

    @abstractmethod
    def finalize(self) -> None:
        """Deduplicate edges by ``(s, p, o)``, compute aggregate confidence
        (noisy-OR) and ``assertion_count``, and mark demoted edges."""

    @abstractmethod
    def stats(self) -> dict:
        """``{entities, edges, demoted}`` counts for the current graph."""

    # -- query --
    @abstractmethod
    def get_entity(self, ref: str) -> dict | None:
        """``{type, pages, edges}`` for ``ref``, or ``None`` if unknown."""

    @abstractmethod
    def neighbors(self, ref: str, predicate: str | None = None, direction: str = "out") -> list[dict]:
        """Adjacent entities via non-demoted edges. ``direction`` in
        ``{"out", "in", "both"}``; optional ``predicate`` filter."""

    @abstractmethod
    def traverse(
        self, ref: str, predicate: str | None = None, depth: int = 2,
        include_demoted: bool = False,
    ) -> list[dict]:
        """Entities reachable from ``ref`` within ``depth`` hops, each with its
        path and the connecting predicates. Depth-bounded, cycle-safe,
        deterministic."""


# --- SQLite backend --------------------------------------------------------


class SqliteGraphBackend(GraphBackend):
    """Embedded-SQLite GraphBackend: an edges table with recursive-CTE traversal.

    No separate server, no dependency beyond the SQLite already used for search
    and state. Because the graph is a cache, this engine carries no canonical
    data and is trivially swappable.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._pending: list[tuple] = []  # (s, p, o, page, conf, active)

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE IF NOT EXISTS entities (ref TEXT PRIMARY KEY, type TEXT)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                s TEXT, p TEXT, o TEXT,
                source_pages TEXT,         -- JSON list of page ids
                assertion_count INTEGER,
                confidence REAL,
                demoted INTEGER DEFAULT 0
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS edges_s ON edges(s)")
        conn.execute("CREATE INDEX IF NOT EXISTS edges_o ON edges(o)")
        return conn

    # -- build --

    def clear(self) -> None:
        conn = self._connect()
        try:
            conn.execute("DELETE FROM entities")
            conn.execute("DELETE FROM edges")
            conn.commit()
        finally:
            conn.close()
        self._pending = []

    def add_entity(self, ref: str, type: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO entities (ref, type) VALUES (?, ?) ON CONFLICT(ref) DO NOTHING",
                (ref, type),
            )
            conn.commit()
        finally:
            conn.close()

    def add_edge(
        self, s: str, p: str, o: str, source_page: str, page_confidence: float,
        page_active: bool = True,
    ) -> None:
        self._pending.append((s, p, o, source_page, page_confidence, page_active))

    def finalize(self) -> None:
        # Group raw assertions by (s, p, o), one contribution per source page.
        groups: dict[tuple[str, str, str], dict[str, tuple[float, bool]]] = {}
        for s, p, o, page, conf, active in self._pending:
            groups.setdefault((s, p, o), {})[page] = (conf, active)

        rows = []
        for (s, p, o), per_page in groups.items():
            confs = [c for c, _ in per_page.values()]
            noisy_or = 1.0 - math.prod(1.0 - c for c in confs)
            demoted = 0 if any(active for _, active in per_page.values()) else 1
            rows.append(
                (s, p, o, json.dumps(sorted(per_page)), len(per_page), noisy_or, demoted)
            )

        conn = self._connect()
        try:
            conn.execute("DELETE FROM edges")
            conn.executemany(
                "INSERT INTO edges (s, p, o, source_pages, assertion_count, confidence, demoted) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.commit()
        finally:
            conn.close()
        self._pending = []

    def stats(self) -> dict:
        conn = self._connect()
        try:
            entities = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
            edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
            demoted = conn.execute("SELECT COUNT(*) FROM edges WHERE demoted = 1").fetchone()[0]
        finally:
            conn.close()
        return {"entities": entities, "edges": edges, "demoted": demoted}

    # -- query --

    @staticmethod
    def _edge_dict(row: sqlite3.Row) -> dict:
        return {
            "s": row["s"],
            "p": row["p"],
            "o": row["o"],
            "source_pages": json.loads(row["source_pages"]),
            "assertion_count": row["assertion_count"],
            "confidence": row["confidence"],
            "demoted": bool(row["demoted"]),
        }

    def get_entity(self, ref: str) -> dict | None:
        conn = self._connect()
        try:
            erow = conn.execute("SELECT type FROM entities WHERE ref = ?", (ref,)).fetchone()
            if erow is None:
                return None
            edge_rows = conn.execute(
                "SELECT * FROM edges WHERE s = ? OR o = ? ORDER BY s, p, o", (ref, ref)
            ).fetchall()
        finally:
            conn.close()
        edges = [self._edge_dict(r) for r in edge_rows]
        pages = sorted({pid for e in edges for pid in e["source_pages"]})
        return {"type": erow["type"], "pages": pages, "edges": edges}

    def neighbors(self, ref: str, predicate: str | None = None, direction: str = "out") -> list[dict]:
        clauses = ["demoted = 0"]
        if direction == "out":
            clauses.append("s = :ref")
        elif direction == "in":
            clauses.append("o = :ref")
        elif direction == "both":
            clauses.append("(s = :ref OR o = :ref)")
        else:
            raise ValueError(f"direction must be out|in|both, got {direction!r}")
        params: dict = {"ref": ref}
        if predicate is not None:
            clauses.append("p = :predicate")
            params["predicate"] = predicate

        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT * FROM edges WHERE {' AND '.join(clauses)} ORDER BY s, p, o", params
            ).fetchall()
        finally:
            conn.close()

        out = []
        for r in rows:
            edge = self._edge_dict(r)
            neighbor = edge["o"] if edge["s"] == ref else edge["s"]
            out.append({
                "ref": neighbor,
                "predicate": edge["p"],
                "direction": "out" if edge["s"] == ref else "in",
                "confidence": edge["confidence"],
                "source_pages": edge["source_pages"],
            })
        return out

    def traverse(
        self, ref: str, predicate: str | None = None, depth: int = 2,
        include_demoted: bool = False,
    ) -> list[dict]:
        # Recursive CTE walk over outgoing edges. The path is a '|'-delimited
        # string of refs (refs never contain '|'), so `instr` gives a cycle-safe,
        # cheap "already visited on this path" check. Deterministic via ORDER BY.
        sql = """
            WITH RECURSIVE walk(node, depth, path, preds) AS (
                SELECT :ref, 0, '|' || :ref || '|', ''
                UNION ALL
                SELECT e.o, w.depth + 1, w.path || e.o || '|', w.preds || e.p || '|'
                FROM walk w
                JOIN edges e ON e.s = w.node
                WHERE w.depth < :depth
                  AND (:include_demoted OR e.demoted = 0)
                  AND (:predicate IS NULL OR e.p = :predicate)
                  AND instr(w.path, '|' || e.o || '|') = 0
            )
            SELECT node, depth, path, preds FROM walk WHERE depth > 0
            ORDER BY depth, path
        """
        params = {
            "ref": ref,
            "depth": depth,
            "predicate": predicate,
            "include_demoted": 1 if include_demoted else 0,
        }
        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        results = []
        for r in rows:
            path = [p for p in r["path"].split("|") if p]
            preds = [p for p in r["preds"].split("|") if p]
            results.append({"ref": r["node"], "depth": r["depth"], "path": path, "predicates": preds})
        return results


# --- Factory ---------------------------------------------------------------


def get_graph_backend() -> GraphBackend:
    """Return the configured GraphBackend (``config.GRAPH_BACKEND``).

    This is the one place engines are chosen. Adding a Tier-B backend means
    implementing :class:`GraphBackend` and registering it here — nothing in
    ingest/search/mcp_server/cli changes.
    """
    backend = config.GRAPH_BACKEND
    if backend == "sqlite":
        return SqliteGraphBackend(config.INDEX_DIR / "graph.db")
    raise ValueError(f"unknown graph backend {backend!r} (set WIKI_GRAPH_BACKEND)")


# --- Projection from Markdown ----------------------------------------------


def _entity_type(ref: str) -> str | None:
    """The entity type of a ``type:value`` ref if it is a valid entity, else None."""
    try:
        return vocab.normalize_ref(ref).split(":", 1)[0]
    except ValueError:
        return None


def rebuild_graph(now: datetime | None = None) -> dict:
    """Clear and repopulate the graph from every page, then finalize.

    Returns ``{entities, edges, demoted}``. ``now`` is injectable so the derived
    edge confidences are deterministic for a given corpus.
    """
    backend = get_graph_backend()
    backend.clear()

    for page in store.list_pages():
        conf, _ = confidence.compute_confidence(
            page, access=state.get_access(page.id), now=now
        )
        active = page.status == "active" and page.superseded_by is None

        # Entities from entity-typed tags.
        for tag in page.tags:
            etype = _entity_type(tag)
            if etype is not None:
                backend.add_entity(vocab.normalize_ref(tag), etype)

        # Typed relation edges.
        for rel in page.relations:
            if not {"s", "p", "o"} <= rel.keys():
                continue
            backend.add_entity(rel["s"], rel["s"].split(":", 1)[0])
            backend.add_entity(rel["o"], rel["o"].split(":", 1)[0])
            backend.add_edge(rel["s"], rel["p"], rel["o"], page.id, conf, active)

        # Page-level structural edges (between page nodes).
        this = page_ref(page.id)
        backend.add_entity(this, PAGE_NODE_TYPE)
        if page.supersedes:
            target = page_ref(page.supersedes)
            backend.add_entity(target, PAGE_NODE_TYPE)
            backend.add_edge(this, "supersedes", target, page.id, conf, active)
        for other in page.contradicts:
            target = page_ref(other)
            backend.add_entity(target, PAGE_NODE_TYPE)
            backend.add_edge(this, "contradicts", target, page.id, conf, active)

    backend.finalize()
    return backend.stats()
