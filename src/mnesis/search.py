"""SQLite FTS5 keyword search — a rebuildable projection of the Markdown pages.

Honors the canonical-vs-cache invariant (CLAUDE.md §2.1, §8): the Markdown
pages are the single source of truth and this index stores **nothing** that is
not derivable from them. The DB at ``wiki/.index/wiki.db`` is gitignored and is
fully reconstructable by :func:`rebuild` from ``store.list_pages()`` alone.

Ranking is plain BM25 via FTS5's ``bm25()``. Vector similarity, embeddings, and
graph traversal are explicitly out of scope for the PoC (Phase 5 / Phase 3).
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import config, store

# Column layout of the FTS5 table; body is index 3 (used for snippets).
_COLUMNS = ("id", "title", "tags", "body")
_BODY_COL = 3

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
    score: float  # BM25: lower is a better match (FTS5 convention)
    snippet: str


def _db_path() -> Path:
    return config.INDEX_DIR / "wiki.db"


def _assert_fts5(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("CREATE VIRTUAL TABLE temp.__fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE temp.__fts5_probe")
    except sqlite3.OperationalError as exc:
        raise RuntimeError(_FTS5_REMEDIATION) from exc


def _connect() -> sqlite3.Connection:
    """Open the index DB, verifying FTS5 and ensuring the schema exists."""
    config.INDEX_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_db_path())
    _assert_fts5(conn)
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS pages "
        "USING fts5(id, title, tags, body, tokenize='porter unicode61')"
    )
    return conn


def _row_for(page: store.Page) -> tuple[str, str, str, str]:
    return (page.id, page.title, " ".join(page.tags), page.body)


def rebuild() -> int:
    """Drop and repopulate the index from every Markdown page. Returns the count.

    This is the source-of-truth -> cache projection. Pages are inserted in
    ``store.list_pages()`` order (sorted by id), so the resulting index — and
    therefore BM25 scores and snippets — is deterministic and identical across
    rebuilds.
    """
    conn = _connect()
    try:
        conn.execute("DROP TABLE IF EXISTS pages")
        conn.execute(
            "CREATE VIRTUAL TABLE pages "
            "USING fts5(id, title, tags, body, tokenize='porter unicode61')"
        )
        pages = store.list_pages()
        conn.executemany(
            "INSERT INTO pages (id, title, tags, body) VALUES (?, ?, ?, ?)",
            [_row_for(p) for p in pages],
        )
        conn.commit()
        return len(pages)
    finally:
        conn.close()


def upsert(page: store.Page) -> None:
    """Incrementally (re)index a single page — call after a write."""
    conn = _connect()
    try:
        conn.execute("DELETE FROM pages WHERE id = ?", (page.id,))
        conn.execute(
            "INSERT INTO pages (id, title, tags, body) VALUES (?, ?, ?, ?)",
            _row_for(page),
        )
        conn.commit()
    finally:
        conn.close()


def _to_match_query(query: str) -> str | None:
    """Build a safe FTS5 MATCH expression: alnum tokens, quoted, implicit AND."""
    tokens = re.findall(r"\w+", query.lower())
    if not tokens:
        return None
    return " ".join(f'"{t}"' for t in tokens)


def search(query: str, limit: int = 10) -> list[SearchHit]:
    """Return up to ``limit`` BM25-ranked hits for ``query`` (best first)."""
    match = _to_match_query(query)
    if match is None:
        return []
    conn = _connect()
    try:
        rows = conn.execute(
            f"""
            SELECT id,
                   title,
                   bm25(pages) AS score,
                   snippet(pages, {_BODY_COL}, '[', ']', '…', 12) AS snip
            FROM pages
            WHERE pages MATCH ?
            ORDER BY bm25(pages)
            LIMIT ?
            """,
            (match, limit),
        ).fetchall()
    finally:
        conn.close()
    return [SearchHit(id=r[0], title=r[1], score=r[2], snippet=r[3]) for r in rows]
