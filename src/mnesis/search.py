"""SQLite FTS5 keyword search, blended with Phase-2 confidence (tenant-scoped).

Honors the canonical-vs-cache invariant (CLAUDE.md §2.1, §8): the Markdown pages
are the single source of truth and the search index (``<tenant>/.cache/wiki.db``)
is a rebuildable projection. Each indexed row caches the page's **confidence** (and
``computed_at``) in UNINDEXED columns — derived state that lives here, never in
Markdown. The confidence's Markdown-derived part is reproducible; its access boost
comes from the durable state store (`state.py`), which ``rebuild()`` never clears.

Like the rest of the store layer it is **tenant-scoped by construction**: a
:class:`SearchIndex` is built from a :class:`~mnesis.tenancy.TenantContext`, indexes
only that tenant's pages into that tenant's own ``wiki.db``, and the module-level
functions delegate to one over the active tenant (fail-closed).
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import authz, confidence, tenancy
from .state import StateStore
from .store import Page, Store, now_iso
from .tenancy import VaultContext

# Indexed text columns, then UNINDEXED cached derived state. body is index 3.
# ``type`` (the OKF concept type = Mnesis ``kind``) is indexed after ``body`` so a
# corpus is searchable by concept type; it never contains the knowledge terms, so it
# does not change ranking for content queries (OKF4).
_BODY_COL = 3

# How many surfaced hits get their access recorded + confidence reindexed.
_ACCESS_TOP_N = 3

_SCHEMA = (
    "fts5(id, title, tags, body, type, "
    "status UNINDEXED, confidence UNINDEXED, computed_at UNINDEXED, "
    "tokenize='porter unicode61')"
)

_FTS5_REMEDIATION = (
    "SQLite FTS5 is not available in this Python's sqlite3 build. "
    "Install a Python whose sqlite3 is compiled with SQLITE_ENABLE_FTS5 "
    "(e.g. the python.org or uv-managed CPython, or `brew install sqlite` and "
    "rebuild Python against it). FTS5 is required for keyword search."
)

_EXPECTED_COLUMNS = ["id", "title", "tags", "body", "type", "status", "confidence", "computed_at"]

_INSERT_SQL = (
    "INSERT INTO pages (id, title, tags, body, type, status, confidence, computed_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)


@dataclass
class SearchHit:
    id: str
    title: str
    snippet: str
    bm25_score: float  # raw FTS5 bm25 (lower = better match)
    confidence: float  # cached [0,1] confidence
    final_score: float  # blended rank score (higher = better)
    status: str  # active | stale
    graph_proximity: float = 0.0  # additive graph boost (Phase 3); 0 for keyword-only
    grounding: dict | None = None  # for graph-reached hits: the connecting edge/page


def _assert_fts5(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("CREATE VIRTUAL TABLE temp.__fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE temp.__fts5_probe")
    except sqlite3.OperationalError as exc:
        raise RuntimeError(_FTS5_REMEDIATION) from exc


class SearchIndex:
    """The FTS5 search index for ONE vault (its own ``.cache/wiki.db``)."""

    def __init__(self, ctx: VaultContext) -> None:
        if not isinstance(ctx, VaultContext):
            raise TypeError(f"SearchIndex requires a VaultContext; got {type(ctx).__name__}")
        self.ctx = ctx
        self._store = Store(ctx)
        self._state = StateStore(ctx)

    def _db_path(self) -> Path:
        return self.ctx.cache_path("wiki.db")

    def _connect(self) -> sqlite3.Connection:
        """Open the index DB, verifying FTS5 and ensuring the current schema.

        If an existing ``pages`` table predates the current schema, it is dropped
        and recreated — safe, because the index is a rebuildable cache.
        """
        self.ctx.cache_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path())
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        _assert_fts5(conn)
        conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS pages USING {_SCHEMA}")
        cols = [r[1] for r in conn.execute("PRAGMA table_info(pages)").fetchall()]
        if cols != _EXPECTED_COLUMNS:
            conn.execute("DROP TABLE IF EXISTS pages")
            conn.execute(f"CREATE VIRTUAL TABLE pages USING {_SCHEMA}")
            conn.commit()
        return conn

    def _cached_confidence(self, page: Page) -> float:
        score, _ = confidence.compute_confidence(page, access=self._state.get_access(page.id))
        return score

    def _index_row(self, page: Page) -> tuple:
        return (
            page.id,
            page.title,
            " ".join(page.tags),
            page.body,        # clean prose (OKF cross-links stripped on read)
            page.kind,        # OKF `type` — indexed but never carries the knowledge terms
            page.status,
            self._cached_confidence(page),
            now_iso(),
        )

    def rebuild(self) -> int:
        """Drop and repopulate the index from every Markdown page. Returns count.

        Reproducible in its Markdown-derived parts (bm25, snippets); the cached
        confidence additionally reflects the durable state store, which this reads
        but never clears.
        """
        conn = self._connect()
        try:
            conn.execute("DROP TABLE IF EXISTS pages")
            conn.execute(f"CREATE VIRTUAL TABLE pages USING {_SCHEMA}")
            pages = self._store.list_pages()
            conn.executemany(_INSERT_SQL, [self._index_row(p) for p in pages])
            conn.commit()
            return len(pages)
        finally:
            conn.close()

    def upsert(self, page) -> None:
        """Incrementally (re)index a single page, recomputing its cached confidence."""
        conn = self._connect()
        try:
            conn.execute("DELETE FROM pages WHERE id = ?", (page.id,))
            conn.execute(_INSERT_SQL, self._index_row(page))
            conn.commit()
        finally:
            conn.close()

    def indexed_ids(self) -> set[str]:
        """The set of page ids currently present in the search index (freshness probe)."""
        conn = self._connect()
        try:
            rows = conn.execute("SELECT id FROM pages").fetchall()
        finally:
            conn.close()
        return {r[0] for r in rows}

    def record_and_reindex(self, page_id: str) -> None:
        """Record one access to ``page_id`` and refresh its cached confidence.

        Reinforcement on read (CLAUDE.md §8). Best-effort: never blocks/fails a query.
        """
        try:
            self._state.record_access(page_id)
            self.upsert(self._store.read_page(page_id))
        except Exception:
            pass

    def search(self, query: str, limit: int = 10, include_stale: bool = False) -> list[SearchHit]:
        """Confidence-blended keyword search (see module docstring)."""
        match = _to_match_query(query)
        if match is None:
            return []
        sql = (
            f"SELECT id, title, snippet(pages, {_BODY_COL}, '[', ']', '…', 12) AS snip, "
            "bm25(pages) AS bm25, status, confidence "
            "FROM pages WHERE pages MATCH ?"
        )
        if not include_stale:
            sql += " AND status != 'stale'"
        conn = self._connect()
        try:
            rows = conn.execute(sql, (match,)).fetchall()
        finally:
            conn.close()
        if not rows:
            return []

        # Normalize BM25 to [0,1] relevance (FTS5 bm25 is <= 0; more negative = better).
        relevances = [-row[3] for row in rows]
        max_rel = max(relevances)
        hits: list[SearchHit] = []
        for row, rel in zip(rows, relevances):
            conf = float(row[5]) if row[5] is not None else 0.0
            bm25_norm = (rel / max_rel) if max_rel > 0 else 1.0
            final = bm25_norm * (0.5 + 0.5 * conf)
            hits.append(
                SearchHit(
                    id=row[0],
                    title=row[1],
                    snippet=row[2],
                    bm25_score=row[3],
                    confidence=conf,
                    final_score=final,
                    status=row[4],
                )
            )
        # Visibility (T4): when a principal is bound, drop pages it may not see —
        # a private page never reaches search results for a non-owner.
        visible = authz.active_visible_page_ids()
        if visible is not None:
            hits = [h for h in hits if h.id in visible]
        hits.sort(key=lambda h: (-h.final_score, h.bm25_score, h.id))
        return hits[:limit]


# A minimal English stopword set so natural-language questions ("what does X
# use?") don't force every page to contain function words.
_STOPWORDS = frozenset(
    """
    a an the of to in on at by for from into with as is are was were be been being
    do does did what which who whom whose how why when where whether
    this that these those it its and or not no me my we our you your i
    about tell show give find list please can could would should
    """.split()
)


def _to_match_query(query: str) -> str | None:
    """Build a safe FTS5 MATCH expression for keyword/NL retrieval.

    Tokens are **OR-ed and prefix-matched**, so a natural-language question
    retrieves relevant pages instead of requiring every word to appear, and
    morphological variants match. Each token is quoted to neutralize FTS5 operators.
    """
    tokens = re.findall(r"\w+", query.lower())
    if not tokens:
        return None
    content = [t for t in tokens if len(t) > 1 and t not in _STOPWORDS]
    if not content:  # query was all stopwords/short tokens — fall back to them
        content = [t for t in tokens if len(t) > 1] or tokens
    return " OR ".join(f'"{t}"*' for t in content)


# --- Module-level delegators (over the ACTIVE tenant; fail-closed) ----------


def active_index() -> SearchIndex:
    return SearchIndex(tenancy.current())


def rebuild() -> int:
    return active_index().rebuild()


def upsert(page) -> None:
    active_index().upsert(page)


def indexed_ids() -> set[str]:
    return active_index().indexed_ids()


def record_and_reindex(page_id: str) -> None:
    active_index().record_and_reindex(page_id)


def search(query: str, limit: int = 10, include_stale: bool = False) -> list[SearchHit]:
    return active_index().search(query, limit, include_stale)
