"""Durable, auxiliary state store (Phase 2 foundation).

This is the *second* store under ``wiki/.index/`` — at ``state.db`` — and it is
deliberately **not** a rebuildable cache. It holds state that cannot be derived
from the Markdown pages:

  - **access events** — how often and how recently a page has been read, an
    input to confidence/decay enrichment.
  - **review queue** — contradictions flagged for human/agent review.

Durability contract (CLAUDE.md §8, "Search index vs state store"): it is created
on demand and **must never be cleared by ``search.rebuild()``** (rebuild only
touches the separate ``wiki.db`` search index). Confidence degrades gracefully
to its Markdown-only value if this store is lost.

Phase 1 ships no confidence computation; this module only records the inputs.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config
from .store import now_iso


def _db_path() -> Path:
    return config.INDEX_DIR / "state.db"


def _connect() -> sqlite3.Connection:
    """Open the state DB, creating it and its schema on demand."""
    config.INDEX_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS access (
            page_id       TEXT PRIMARY KEY,
            access_count  INTEGER NOT NULL DEFAULT 0,
            last_accessed TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS review_queue (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            page_a  TEXT NOT NULL,
            page_b  TEXT NOT NULL,
            kind    TEXT NOT NULL,
            detail  TEXT,
            status  TEXT NOT NULL DEFAULT 'open',
            created TEXT NOT NULL
        )
        """
    )
    return conn


# --- Access events ---------------------------------------------------------


def record_access(page_id: str) -> None:
    """Record one read of ``page_id``: increment its count, refresh last-accessed."""
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO access (page_id, access_count, last_accessed)
            VALUES (?, 1, ?)
            ON CONFLICT(page_id) DO UPDATE SET
                access_count = access_count + 1,
                last_accessed = excluded.last_accessed
            """,
            (page_id, now_iso()),
        )
        conn.commit()
    finally:
        conn.close()


def get_access(page_id: str) -> dict | None:
    """Return ``{"count", "last_accessed"}`` for ``page_id``, or ``None`` if unseen."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT access_count, last_accessed FROM access WHERE page_id = ?",
            (page_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {"count": row["access_count"], "last_accessed": row["last_accessed"]}


# --- Review queue ----------------------------------------------------------


def enqueue_contradiction(page_a: str, page_b: str, detail: str = "") -> int:
    """Add an open contradiction review between two pages. Returns the review id."""
    conn = _connect()
    try:
        cur = conn.execute(
            """
            INSERT INTO review_queue (page_a, page_b, kind, detail, status, created)
            VALUES (?, ?, 'contradiction', ?, 'open', ?)
            """,
            (page_a, page_b, detail, now_iso()),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def list_open_reviews() -> list[dict]:
    """All open review-queue entries, oldest first."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, page_a, page_b, kind, detail, status, created "
            "FROM review_queue WHERE status = 'open' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def resolve_review(review_id: int) -> None:
    """Mark a review-queue entry resolved (it drops out of ``list_open_reviews``)."""
    conn = _connect()
    try:
        conn.execute(
            "UPDATE review_queue SET status = 'resolved' WHERE id = ?", (review_id,)
        )
        conn.commit()
    finally:
        conn.close()
