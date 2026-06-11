"""SQLite FTS5 keyword search, blended with Phase-2 confidence.

Honors the canonical-vs-cache invariant (CLAUDE.md §2.1, §8): the Markdown pages
are the single source of truth and the search index (``wiki/.index/wiki.db``) is
a rebuildable projection. Each indexed row caches the page's **confidence** (and
``computed_at``) in UNINDEXED columns — derived state that lives here, never in
Markdown. The confidence's Markdown-derived part is reproducible; its access
boost comes from the durable state store (`state.py`), which ``rebuild()`` never
clears.

Ranking blends normalized BM25 with confidence so well-supported, fresh, often-
read pages rise and stale ones sink. Component scores are returned on every hit
for explainability. Vectors/RRF remain out of scope (Phase 5).
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import config, confidence, state, store

# Indexed text columns, then UNINDEXED cached derived state. body is index 3.
_BODY_COL = 3

# How many surfaced hits get their access recorded + confidence reindexed.
_ACCESS_TOP_N = 3

_SCHEMA = (
    "fts5(id, title, tags, body, "
    "status UNINDEXED, confidence UNINDEXED, computed_at UNINDEXED, "
    "tokenize='porter unicode61')"
)

_FTS5_REMEDIATION = (
    "SQLite FTS5 is not available in this Python's sqlite3 build. "
    "Install a Python whose sqlite3 is compiled with SQLITE_ENABLE_FTS5 "
    "(e.g. the python.org or uv-managed CPython, or `brew install sqlite` and "
    "rebuild Python against it). FTS5 is required for keyword search."
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


def _db_path() -> Path:
    return config.INDEX_DIR / "wiki.db"


def _assert_fts5(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("CREATE VIRTUAL TABLE temp.__fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE temp.__fts5_probe")
    except sqlite3.OperationalError as exc:
        raise RuntimeError(_FTS5_REMEDIATION) from exc


_EXPECTED_COLUMNS = ["id", "title", "tags", "body", "status", "confidence", "computed_at"]


def _connect() -> sqlite3.Connection:
    """Open the index DB, verifying FTS5 and ensuring the (current) schema exists.

    If an existing ``pages`` table predates the current schema, it is dropped and
    recreated — safe, because the index is a rebuildable cache (a later
    ``rebuild()``/``upsert`` repopulates it).
    """
    config.INDEX_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_db_path())
    _assert_fts5(conn)
    conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS pages USING {_SCHEMA}")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(pages)").fetchall()]
    if cols != _EXPECTED_COLUMNS:
        conn.execute("DROP TABLE IF EXISTS pages")
        conn.execute(f"CREATE VIRTUAL TABLE pages USING {_SCHEMA}")
        conn.commit()
    return conn


def _cached_confidence(page: store.Page) -> float:
    """Confidence for the cached column: Markdown inputs + durable access state."""
    score, _ = confidence.compute_confidence(page, access=state.get_access(page.id))
    return score


def _index_row(page: store.Page) -> tuple:
    return (
        page.id,
        page.title,
        " ".join(page.tags),
        page.body,
        page.status,
        _cached_confidence(page),
        store.now_iso(),
    )


_INSERT_SQL = (
    "INSERT INTO pages (id, title, tags, body, status, confidence, computed_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)


def rebuild() -> int:
    """Drop and repopulate the index from every Markdown page. Returns the count.

    Reproducible in its Markdown-derived parts (bm25, snippets); the cached
    confidence additionally reflects the durable state store, which this function
    reads but never clears.
    """
    conn = _connect()
    try:
        conn.execute("DROP TABLE IF EXISTS pages")
        conn.execute(f"CREATE VIRTUAL TABLE pages USING {_SCHEMA}")
        pages = store.list_pages()
        conn.executemany(_INSERT_SQL, [_index_row(p) for p in pages])
        conn.commit()
        return len(pages)
    finally:
        conn.close()


def upsert(page: store.Page) -> None:
    """Incrementally (re)index a single page, recomputing its cached confidence."""
    conn = _connect()
    try:
        conn.execute("DELETE FROM pages WHERE id = ?", (page.id,))
        conn.execute(_INSERT_SQL, _index_row(page))
        conn.commit()
    finally:
        conn.close()


def record_and_reindex(page_id: str) -> None:
    """Record one access to ``page_id`` and refresh its cached confidence.

    Reinforcement on read (CLAUDE.md §8). Best-effort: access tracking must be
    cheap and must never block or fail a query, so all errors are swallowed.
    """
    try:
        state.record_access(page_id)
        upsert(store.read_page(page_id))
    except Exception:
        pass


def _to_match_query(query: str) -> str | None:
    """Build a safe FTS5 MATCH expression: alnum tokens, quoted, implicit AND."""
    tokens = re.findall(r"\w+", query.lower())
    if not tokens:
        return None
    return " ".join(f'"{t}"' for t in tokens)


def search(query: str, limit: int = 10, include_stale: bool = False) -> list[SearchHit]:
    """Confidence-blended keyword search.

    Returns up to ``limit`` hits ordered by ``final_score`` (higher = better),
    which blends normalized BM25 relevance with cached confidence:
    ``final = bm25_norm * (0.5 + 0.5 * confidence)``. Stale pages are excluded
    unless ``include_stale=True``, and (being capped at ``STALE_CAP``) never
    outrank an active page of comparable match.
    """
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
    conn = _connect()
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
    # Best first; deterministic tie-breaks on relevance then id.
    hits.sort(key=lambda h: (-h.final_score, h.bm25_score, h.id))
    return hits[:limit]
