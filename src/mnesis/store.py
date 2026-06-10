"""Canonical Markdown + frontmatter + git store.

This module owns the single source of truth (CLAUDE.md §2.1): every page is a
Markdown file with YAML frontmatter under ``wiki/pages/<id>.md``, and every
mutation is one git commit (CLAUDE.md §2.4). The SQLite search index is a
separate, rebuildable projection and is *not* this module's concern — page
bodies stay clean, human-readable Markdown with no index/search metadata.

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

from . import config

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
    "supersedes",
    "superseded_by",
    "question",
)


def now_iso() -> str:
    """Current UTC time as an ISO 8601 string (microsecond precision, Z suffix)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass
class Page:
    """A canonical wiki page — the in-memory mirror of one ``wiki/pages/<id>.md``.

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
    supersedes: str | None = None
    superseded_by: str | None = None
    question: str | None = None  # digest pages only


# --- Slugs -----------------------------------------------------------------


def slugify(title: str) -> str:
    """Collision-free-*shape* slug of a title (lowercase, hyphenated, alnum-only).

    Uniqueness against existing pages is handled by :func:`make_id`.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug or "page"


def make_id(title: str) -> str:
    """A page id derived from ``title`` that does not collide with an existing page.

    Appends ``-2``, ``-3``, ... when the base slug is already taken.
    """
    base = slugify(title)
    if not page_exists(base):
        return base
    n = 2
    while page_exists(f"{base}-{n}"):
        n += 1
    return f"{base}-{n}"


# --- Paths -----------------------------------------------------------------


def _page_path(page_id: str) -> Path:
    """Resolve the on-disk path for a page id, refusing anything outside PAGES_DIR."""
    if "/" in page_id or "\\" in page_id or page_id in {"", ".", ".."}:
        raise ValueError(f"unsafe page id: {page_id!r}")
    return config.PAGES_DIR / f"{page_id}.md"


def page_exists(page_id: str) -> bool:
    return _page_path(page_id).exists()


# --- Git -------------------------------------------------------------------


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _repo_root(path: Path) -> Path:
    """The git work-tree root containing ``path``."""
    start = path if path.is_dir() else path.parent
    out = subprocess.run(
        ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(out.stdout.strip())


def _ensure_identity(repo_root: Path) -> None:
    """Set a local PoC identity if no user.name/user.email is configured.

    Respects an existing global/local identity (e.g. the user's ~/.gitconfig);
    only fills the gap so runtime commits never fail.
    """
    for key, value in (("user.name", "mnesis PoC"), ("user.email", "mnesis@localhost")):
        result = subprocess.run(
            ["git", "-C", str(repo_root), "config", key],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 or not result.stdout.strip():
            _git(repo_root, "config", key, value)


def _commit(paths: list[Path], message: str) -> None:
    """Stage and commit exactly ``paths`` as a single commit, isolated from any
    other working-tree changes."""
    repo_root = _repo_root(paths[0])
    _ensure_identity(repo_root)
    str_paths = [str(p) for p in paths]
    _git(repo_root, "add", "--", *str_paths)
    _git(repo_root, "commit", "-m", message, "--", *str_paths)


# --- Serialization ---------------------------------------------------------


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


def _write_file(page: Page) -> Path:
    """Refresh ``updated`` and persist the page to disk (no git). Returns its path."""
    config.PAGES_DIR.mkdir(parents=True, exist_ok=True)
    page.updated = now_iso()
    path = _page_path(page.id)
    text = frontmatter.dumps(_to_post(page), sort_keys=False)
    path.write_text(text + "\n", encoding="utf-8")
    return path


# --- Public API ------------------------------------------------------------


def write_page(page: Page) -> Path:
    """Persist ``page`` and commit it as ``mnesis: write <id>``. Returns the path.

    ``updated`` is refreshed in place, so the passed object matches what is on
    disk after the call.
    """
    path = _write_file(page)
    _commit([path], f"mnesis: write {page.id}")
    return path


def write_source(source_ref: str, text: str) -> Path:
    """Persist a (already-redacted) raw source to ``wiki/sources/<ref>.md`` for
    provenance and commit it as ``mnesis: source <ref>``. Returns the path.

    Callers must scrub ``text`` first — this writes verbatim (CLAUDE.md §2.2/§7).
    """
    if "/" in source_ref or "\\" in source_ref or source_ref in {"", ".", ".."}:
        raise ValueError(f"unsafe source ref: {source_ref!r}")
    config.SOURCES_DIR.mkdir(parents=True, exist_ok=True)
    path = config.SOURCES_DIR / f"{source_ref}.md"
    path.write_text(text.rstrip() + "\n", encoding="utf-8")
    _commit([path], f"mnesis: source {source_ref}")
    return path


def read_page(page_id: str) -> Page:
    """Load a page from disk."""
    path = _page_path(page_id)
    if not path.exists():
        raise FileNotFoundError(f"no such page: {page_id}")
    return _from_post(frontmatter.load(str(path)))


def list_pages(status: str | None = None, kind: str | None = None) -> list[Page]:
    """All pages, optionally filtered by ``status`` and/or ``kind``, sorted by id."""
    if not config.PAGES_DIR.exists():
        return []
    pages = [_from_post(frontmatter.load(str(p))) for p in config.PAGES_DIR.glob("*.md")]
    if status is not None:
        pages = [p for p in pages if p.status == status]
    if kind is not None:
        pages = [p for p in pages if p.kind == kind]
    return sorted(pages, key=lambda p: p.id)


def supersede(old_id: str, new_page: Page) -> Path:
    """Replace ``old_id`` with ``new_page`` (Phase-2 lifecycle seam).

    Links both directions — the new page ``supersedes`` the old, the old page is
    flipped to ``status: stale`` with ``superseded_by`` set — and records the pair
    in a single commit. Stale pages are deprioritised, never deleted (CLAUDE.md §12).
    """
    old = read_page(old_id)
    new_page.supersedes = old_id
    old.status = "stale"
    old.superseded_by = new_page.id

    new_path = _write_file(new_page)
    old_path = _write_file(old)
    _commit([new_path, old_path], f"mnesis: supersede {old_id} -> {new_page.id}")
    return new_path
