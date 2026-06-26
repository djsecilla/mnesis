"""Canonical Markdown + frontmatter + git store (tenant-scoped).

This module owns the single source of truth (CLAUDE.md §2.1): every page is a
Markdown file with YAML frontmatter under ``tenants/<id>/pages/<id>.md``, and
every mutation is one git commit in that tenant's own repo (CLAUDE.md §2.4). The
SQLite caches are a separate, rebuildable projection and are *not* this module's
concern — page bodies stay clean, human-readable Markdown.

**The store is tenant-scoped by construction (CLAUDE.md §16):** all filesystem and
git operations live on :class:`Store`, which is built from a
:class:`mnesis.tenancy.TenantContext`. There is no global store. The module-level
functions (``write_page``, ``read_page``, …) are thin delegators to a ``Store``
over the *active* tenant context (:func:`mnesis.tenancy.current`), which fails
closed when no tenant is bound — so the store can never be reached without first
resolving a tenant at a boundary.

Frontmatter field names here are authoritative against CLAUDE.md §4. If a field
name changes, CLAUDE.md must change in the same commit.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path

import frontmatter

from . import tenancy
from .tenancy import TenantContext

# Frontmatter keys, in the schema order of CLAUDE.md §4. ``body`` is the post
# content, not a frontmatter key; ``question`` is emitted for digest pages only.
_META_KEYS = (
    "id",
    "title",
    "created",
    "updated",
    "sources",
    "source_count",
    "last_confirmed",
    "tags",
    "kind",
    "status",
    "owner_principal",
    "visibility",
    "supersedes",
    "superseded_by",
    "contradicts",
    "decay_class",
    "relations",
    "question",
)


def now_iso() -> str:
    """Current UTC time as an ISO 8601 string (microsecond precision, Z suffix)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass
class Page:
    """A canonical wiki page — the in-memory mirror of one ``pages/<id>.md``.

    Field names and meanings track CLAUDE.md §4 exactly. ``body`` holds the clean
    Markdown prose (the frontmatter post content); every other field except it is
    serialized into the YAML frontmatter block.
    """

    id: str
    title: str
    body: str = ""
    created: str = field(default_factory=now_iso)
    updated: str = field(default_factory=now_iso)
    sources: list[str] = field(default_factory=list)
    source_count: int = 1
    last_confirmed: str = field(default_factory=now_iso)
    tags: list[str] = field(default_factory=list)
    kind: str = "fact"  # fact | digest | note
    status: str = "active"  # active | stale
    owner_principal: str | None = None  # T4: the principal that created the page (None = unowned/legacy)
    visibility: str = "shared"  # T4: shared (all principals in the tenant) | private (owner-only)
    supersedes: str | None = None
    superseded_by: str | None = None
    contradicts: list[str] = field(default_factory=list)  # Phase 2: conflicting page ids
    decay_class: str | None = None  # Phase 2: optional override of inferred decay class
    relations: list[dict] = field(default_factory=list)  # Phase 3: typed {s,p,o} edges
    question: str | None = None  # digest pages only


# --- Slugs (pure) ----------------------------------------------------------


def slugify(title: str) -> str:
    """Collision-free-*shape* slug of a title (lowercase, hyphenated, alnum-only).

    Uniqueness against existing pages is handled by :meth:`Store.make_id`.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug or "page"


# --- Serialization (pure) --------------------------------------------------


def _to_post(page: Page) -> frontmatter.Post:
    meta: dict = {}
    for key in _META_KEYS:
        if key == "question" and page.question is None:
            continue  # digest-only field; keep fact/note frontmatter clean
        meta[key] = getattr(page, key)
    return frontmatter.Post(page.body.strip(), **meta)


def _from_post(post: frontmatter.Post) -> Page:
    meta = post.metadata
    known = {f.name for f in fields(Page)}
    kwargs = {k: v for k, v in meta.items() if k in known}
    kwargs["body"] = post.content.strip()
    return Page(**kwargs)


# --- The tenant-scoped store ----------------------------------------------


class Store:
    """The canonical Markdown + git store for ONE tenant.

    Constructed from a :class:`~mnesis.tenancy.TenantContext`; every path it touches
    is resolved against (and guarded within) that tenant's root, and every commit
    lands in that tenant's own git repo. There is no way to build a ``Store`` that
    spans tenants.
    """

    def __init__(self, ctx: TenantContext) -> None:
        if not isinstance(ctx, TenantContext):
            raise TypeError(
                "Store requires a TenantContext (the store is tenant-scoped by "
                f"construction); got {type(ctx).__name__}"
            )
        self.ctx = ctx

    # -- paths --------------------------------------------------------------

    def _page_path(self, page_id: str) -> Path:
        return self.ctx.page_path(page_id)  # guarded: refuses traversal / escape

    def page_exists(self, page_id: str) -> bool:
        return self._page_path(page_id).exists()

    def make_id(self, title: str) -> str:
        """A page id derived from ``title`` that does not collide with an existing
        page in this tenant. Appends ``-2``, ``-3``, … when the base slug is taken."""
        base = slugify(title)
        if not self.page_exists(base):
            return base
        n = 2
        while self.page_exists(f"{base}-{n}"):
            n += 1
        return f"{base}-{n}"

    # -- git ----------------------------------------------------------------

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.ctx.git_root), *args],
            check=True,
            capture_output=True,
            text=True,
        )

    def _ensure_identity(self) -> None:
        for key, value in (("user.name", "mnesis PoC"), ("user.email", "mnesis@localhost")):
            result = subprocess.run(
                ["git", "-C", str(self.ctx.git_root), "config", key],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0 or not result.stdout.strip():
                self._git("config", key, value)

    def _commit(self, paths: list[Path], message: str) -> None:
        """Stage and commit exactly ``paths`` in this tenant's repo, isolated from
        any other working-tree changes."""
        tenancy.init_git(self.ctx)  # idempotent: ensures the tenant repo exists
        self._ensure_identity()
        str_paths = [str(p) for p in paths]
        self._git("add", "--", *str_paths)
        self._git("commit", "-m", message, "--", *str_paths)

    # -- serialization to disk ---------------------------------------------

    def _write_file(self, page: Page) -> Path:
        """Refresh ``updated`` and persist the page to disk (no git). Returns path."""
        self.ctx.pages_dir.mkdir(parents=True, exist_ok=True)
        page.updated = now_iso()
        path = self._page_path(page.id)
        text = frontmatter.dumps(_to_post(page), sort_keys=False)
        path.write_text(text + "\n", encoding="utf-8")
        return path

    # -- public API ---------------------------------------------------------

    def write_page(self, page: Page, message: str | None = None) -> Path:
        """Persist ``page`` and commit it. Returns the path.

        The commit message defaults to ``mnesis: write <id>``; callers performing a
        lifecycle update (reinforce, contradiction cross-link) pass their own.
        ``updated`` is refreshed in place, so the passed object matches disk.
        """
        path = self._write_file(page)
        self._commit([path], message or f"mnesis: write {page.id}")
        return path

    def write_source(self, source_ref: str, text: str) -> Path:
        """Persist a (already-redacted) raw source to ``sources/<ref>.md`` for
        provenance and commit it as ``mnesis: source <ref>``. Returns the path.

        Callers must scrub ``text`` first — this writes verbatim (CLAUDE.md §2.2/§7).
        """
        self.ctx.sources_dir.mkdir(parents=True, exist_ok=True)
        path = self.ctx.source_path(source_ref)  # guarded
        path.write_text(text.rstrip() + "\n", encoding="utf-8")
        self._commit([path], f"mnesis: source {source_ref}")
        return path

    def read_page(self, page_id: str) -> Page:
        """Load a page from disk."""
        path = self._page_path(page_id)
        if not path.exists():
            raise FileNotFoundError(f"no such page: {page_id}")
        return _from_post(frontmatter.load(str(path)))

    def list_pages(self, status: str | None = None, kind: str | None = None) -> list[Page]:
        """All pages, optionally filtered by ``status`` and/or ``kind``, by id."""
        if not self.ctx.pages_dir.exists():
            return []
        pages = [_from_post(frontmatter.load(str(p))) for p in self.ctx.pages_dir.glob("*.md")]
        if status is not None:
            pages = [p for p in pages if p.status == status]
        if kind is not None:
            pages = [p for p in pages if p.kind == kind]
        return sorted(pages, key=lambda p: p.id)

    def supersede(self, old_id: str, new_page: Page) -> Path:
        """Replace ``old_id`` with ``new_page`` (Phase-2 lifecycle seam).

        Links both directions — the new page ``supersedes`` the old, the old page is
        flipped to ``status: stale`` with ``superseded_by`` set — and records the
        pair in a single commit. Stale pages are deprioritised, never deleted.

        Superseding also **resolves any mutual contradiction** between the two pages:
        each is removed from the other's ``contradicts`` list (lifting the kept
        page's ``contradiction_factor``).
        """
        old = self.read_page(old_id)
        new_page.supersedes = old_id
        old.status = "stale"
        old.superseded_by = new_page.id
        new_page.contradicts = [c for c in new_page.contradicts if c != old_id]
        old.contradicts = [c for c in old.contradicts if c != new_page.id]

        new_path = self._write_file(new_page)
        old_path = self._write_file(old)
        self._commit([new_path, old_path], f"mnesis: supersede {old_id} -> {new_page.id}")
        return new_path


# --- Module-level delegators (over the ACTIVE tenant; fail-closed) ----------
# These keep the call sites of ingest/search/cli/etc. tenant-agnostic: they resolve
# the active TenantContext (bound at a boundary) and operate a Store over it.
# Reaching them with no tenant bound raises NoTenantContextError — there is no
# ambient/global store.


def active_store() -> Store:
    """A Store over the active tenant context (raises if none is bound)."""
    return Store(tenancy.current())


def make_id(title: str) -> str:
    return active_store().make_id(title)


def page_exists(page_id: str) -> bool:
    return active_store().page_exists(page_id)


def write_page(page: Page, message: str | None = None) -> Path:
    return active_store().write_page(page, message)


def write_source(source_ref: str, text: str) -> Path:
    return active_store().write_source(source_ref, text)


def read_page(page_id: str) -> Page:
    return active_store().read_page(page_id)


def list_pages(status: str | None = None, kind: str | None = None) -> list[Page]:
    return active_store().list_pages(status, kind)


def supersede(old_id: str, new_page: Page) -> Path:
    return active_store().supersede(old_id, new_page)
