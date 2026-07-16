"""Durable, auxiliary state store (Phase 2 foundation), tenant-scoped.

This is the second store under a tenant's ``.cache/`` — at ``state.db`` — and it is
deliberately **not** a rebuildable cache. It holds state that cannot be derived
from the Markdown pages:

  - **access events** — how often and how recently a page has been read, an input
    to confidence/decay enrichment.
  - **review queue** — contradictions flagged for human/agent review.

Durability contract (CLAUDE.md §8): created on demand and **never cleared by
``search.rebuild()``**. It lives under ``.cache/`` for locality but is conceptually
separate from the rebuildable caches; confidence degrades gracefully to its
Markdown-only value if it is lost.

Like the rest of the store layer it is **tenant-scoped by construction**: a
:class:`StateStore` is built from a :class:`~mnesis.tenancy.TenantContext`, and the
module-level functions delegate to one over the active tenant (fail-closed).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import confidence, tenancy
from .store import Page, now_iso
from .tenancy import VaultContext


class StateStore:
    """The durable access + review-queue store for ONE vault."""

    def __init__(self, ctx: VaultContext) -> None:
        if not isinstance(ctx, VaultContext):
            raise TypeError(f"StateStore requires a VaultContext; got {type(ctx).__name__}")
        self.ctx = ctx

    def _db_path(self) -> Path:
        return self.ctx.cache_path("state.db")

    def _connect(self) -> sqlite3.Connection:
        """Open the state DB, creating it and its schema on demand."""
        self.ctx.cache_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path())
        conn.execute("PRAGMA journal_mode=WAL")  # concurrent reads with the running server
        conn.execute("PRAGMA busy_timeout=5000")
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

    # -- access events ------------------------------------------------------

    def record_access(self, page_id: str) -> None:
        """Record one read of ``page_id``: increment count, refresh last-accessed."""
        conn = self._connect()
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

    def get_access(self, page_id: str) -> dict | None:
        """Return ``{"count", "last_accessed"}`` for ``page_id`` or ``None``."""
        conn = self._connect()
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

    # -- review queue -------------------------------------------------------

    def enqueue_contradiction(self, page_a: str, page_b: str, detail: str = "") -> int:
        """Add an open contradiction review between two pages. Returns the review id."""
        conn = self._connect()
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

    def list_open_reviews(self) -> list[dict]:
        """All open review-queue entries, oldest first."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, page_a, page_b, kind, detail, status, created "
                "FROM review_queue WHERE status = 'open' ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    def resolve_review(self, review_id: int) -> None:
        """Mark a review-queue entry resolved (it drops out of list_open_reviews)."""
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE review_queue SET status = 'resolved' WHERE id = ?", (review_id,)
            )
            conn.commit()
        finally:
            conn.close()


# --- Module-level delegators (over the ACTIVE tenant; fail-closed) ----------


def active_state() -> StateStore:
    return StateStore(tenancy.current())


def record_access(page_id: str) -> None:
    active_state().record_access(page_id)


def get_access(page_id: str) -> dict | None:
    return active_state().get_access(page_id)


def enqueue_contradiction(page_a: str, page_b: str, detail: str = "") -> int:
    return active_state().enqueue_contradiction(page_a, page_b, detail)


def list_open_reviews() -> list[dict]:
    return active_state().list_open_reviews()


def resolve_review(review_id: int) -> None:
    active_state().resolve_review(review_id)


# --- Derived helpers (combine the review/access state; used by every surface) --


def open_contradiction_ids() -> set[str]:
    """The page ids that appear in an open contradiction review (either side)."""
    ids: set[str] = set()
    for r in list_open_reviews():
        ids.add(r["page_a"])
        ids.add(r["page_b"])
    return ids


def page_confidence(page: Page) -> float:
    """A page's confidence enriched with its live access boost from this store.

    The one place the surfaces (MCP, web) fold the durable access record into the
    otherwise-pure confidence computation, so the "confidence + access" pairing
    lives here rather than being re-derived per surface.
    """
    return confidence.compute_confidence(page, access=get_access(page.id))[0]
