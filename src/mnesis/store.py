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
from pathlib import Path

import frontmatter

from . import okf, tenancy
from .config import now_iso  # noqa: F401 — re-exported for callers that import from store
from .tenancy import TenantContext

#: Reserved OKF filenames that live in the ``pages/`` bundle but are NOT concept pages
#: (a directory listing + the change log). Page enumeration skips them.
RESERVED_PAGE_FILES: frozenset[str] = frozenset(okf.RESERVED_FILES)


class OKFConformanceError(Exception):
    """A write produced a non-OKF-conformant document — refused before commit (OKF2)."""

    def __init__(self, name: str, report: "okf.OKFReport") -> None:
        errs = "; ".join(str(i) for i in report.errors)
        super().__init__(f"{name} is not OKF-conformant: {errs}")
        self.report = report


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


def _to_okf_text(page: Page) -> str:
    """Serialize a page to an OKF-conformant Markdown document (frontmatter + body with
    generated OKF cross-links). Delegates to the OKF contract module."""
    return okf.to_okf_document(page)


def _from_post(post: frontmatter.Post, *, id_hint: str | None = None) -> Page:
    """Reconstruct a :class:`Page` from a (possibly OKF-shaped) frontmatter Post.

    Maps the OKF-core fields back to Mnesis fields (``timestamp`` → ``updated``; ``type``
    → ``kind`` only as a fallback, since ``kind`` is also carried as an extension), takes
    the concept ``id`` from the file path when given (the canonical OKF identity), and
    strips the generated OKF cross-links block so ``page.body`` is the clean prose."""
    meta = post.metadata or {}
    known = {f.name for f in fields(Page)}
    kwargs = {k: v for k, v in meta.items() if k in known}
    # OKF core → Mnesis fields.
    if "timestamp" in meta and "updated" not in meta:
        kwargs["updated"] = meta["timestamp"]
    if "kind" not in kwargs and isinstance(meta.get("type"), str):
        kwargs["kind"] = meta["type"]
    # Path is the canonical concept identity; the `id` frontmatter key is a mere alias.
    if id_hint is not None:
        kwargs["id"] = id_hint
    kwargs["body"] = okf.strip_generated_links(post.content).strip()
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

    # -- serialization to disk (OKF-conformant, OKF2) -----------------------

    def _validate_or_raise(self, text: str, name: str) -> None:
        """OKF conformance gate: refuse a non-conformant document before it is written
        or committed (fail closed). Warnings are allowed; only errors block."""
        report = okf.validate_document(text, path=name)
        if not report.conformant:
            raise OKFConformanceError(name, report)

    def _write_file(self, page: Page) -> Path:
        """Refresh ``updated`` and persist the page as an OKF-conformant document (no
        git). Validated before it touches disk. Returns the path."""
        self.ctx.pages_dir.mkdir(parents=True, exist_ok=True)
        page.updated = now_iso()
        path = self._page_path(page.id)
        text = _to_okf_text(page)
        self._validate_or_raise(text, f"{page.id}.md")
        path.write_text(text + "\n", encoding="utf-8")
        return path

    # -- reserved files (OKF: index.md + log.md) ----------------------------

    def _render_index(self) -> str:
        """A progressive-disclosure directory listing for the ``pages/`` bundle —
        bundle-absolute Markdown links to every concept. **No frontmatter** (OKF)."""
        lines = ["# Index", "", f"{len(self.list_pages())} concepts in this bundle.", ""]
        for p in self.list_pages():
            lines.append(f"- [{p.title}](/{p.id})")
        return "\n".join(lines).rstrip() + "\n"

    def _render_log(self, pending_message: str) -> str:
        """The bundle change history from git — **ISO 8601 date headings** with prose
        entries (OKF). Includes the pending (about-to-be-committed) change at the top,
        since it is not yet in ``git log``."""
        today = now_iso()[:10]
        entries: list[tuple[str, str]] = [(today, pending_message)]
        try:
            out = self._git("log", "--format=%cs%x09%s", "--max-count=500").stdout
            for line in out.splitlines():
                if "\t" in line:
                    date, subject = line.split("\t", 1)
                    entries.append((date.strip(), subject.strip()))
        except subprocess.CalledProcessError:
            pass  # no history yet (first commit) — the pending entry stands alone
        lines = ["# Changelog", ""]
        last_date: str | None = None
        for date, subject in entries:
            if date != last_date:
                lines.append(f"## {date}")
                lines.append("")
                last_date = date
            lines.append(f"- {subject}")
        return "\n".join(lines).rstrip() + "\n"

    def _write_reserved_files(self, pending_message: str) -> list[Path]:
        """Regenerate + validate index.md and log.md (never committed on their own — the
        caller includes them in the page's commit, so commit counts are unchanged)."""
        self.ctx.pages_dir.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        for name, text in (("index.md", self._render_index()),
                           ("log.md", self._render_log(pending_message))):
            self._validate_or_raise(text, name)
            path = self.ctx.pages_dir / name
            path.write_text(text, encoding="utf-8")
            written.append(path)
        return written

    # -- public API ---------------------------------------------------------

    def write_page(self, page: Page, message: str | None = None) -> Path:
        """Persist ``page`` (OKF-conformant) and commit it, regenerating the bundle's
        reserved files (index.md, log.md) in the **same** commit. Returns the path.

        The commit message defaults to ``mnesis: write <id>``; lifecycle callers pass
        their own. ``updated`` is refreshed in place, so the passed object matches disk.
        """
        msg = message or f"mnesis: write {page.id}"
        path = self._write_file(page)
        reserved = self._write_reserved_files(msg)
        self._commit([path, *reserved], msg)
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
        """Load a page from disk (by its path-derived concept id)."""
        if f"{page_id}.md" in RESERVED_PAGE_FILES:
            raise FileNotFoundError(f"no such page: {page_id}")  # reserved file, not a concept
        path = self._page_path(page_id)
        if not path.exists():
            raise FileNotFoundError(f"no such page: {page_id}")
        return _from_post(frontmatter.load(str(path)), id_hint=page_id)

    def list_pages(self, status: str | None = None, kind: str | None = None) -> list[Page]:
        """All concept pages (the reserved index.md/log.md are skipped), optionally
        filtered by ``status`` and/or ``kind``, ordered by id. The concept id is the
        file stem (the OKF path identity)."""
        if not self.ctx.pages_dir.exists():
            return []
        pages = [
            _from_post(frontmatter.load(str(p)), id_hint=p.stem)
            for p in self.ctx.pages_dir.glob("*.md")
            if p.name not in RESERVED_PAGE_FILES
        ]
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

        msg = f"mnesis: supersede {old_id} -> {new_page.id}"
        new_path = self._write_file(new_page)
        old_path = self._write_file(old)
        reserved = self._write_reserved_files(msg)
        self._commit([new_path, old_path, *reserved], msg)
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
