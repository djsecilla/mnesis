"""Tenancy — the isolation primitive (CLAUDE.md §16).

Mnesis is multitenant from the data layer up. Every store object is **constructed
from a :class:`TenantContext`** bound to a per-tenant physical root, so
cross-tenant access is structurally impossible rather than merely checked:

    DATA_ROOT/
      registry.json                # the tenant registry (metadata; OUTSIDE any root)
      tenants/<tenant_id>/         # one tenant's canonical store + its OWN git repo
        pages/                     #   canonical Markdown (tracked)
        sources/                   #   redacted sources (tracked)
        .cache/                    #   rebuildable caches: wiki.db, graph.db, state.db

The guarantees:

  - **No global/ambient store.** There is no module-level store and no function
    that takes a raw cross-tenant path. A path can only be resolved from a
    TenantContext, against that tenant's root.
  - **Paths can never escape the tenant root.** :meth:`TenantContext.resolve`
    refuses traversal (``..``) and absolute escapes, failing closed.
  - **Resolved at boundaries.** The *active* tenant is bound explicitly at a
    boundary (a CLI invocation, an HTTP request) via :func:`use`; :func:`current`
    raises when none is bound, so the store cannot be reached without first
    resolving a tenant. (Later prompts resolve it from credentials/sessions.)

A single-tenant deployment runs transparently as the one ``default`` tenant; the
:func:`migrate_legacy_to_default` step moves an existing single-store layout into
``tenants/default/`` non-destructively and idempotently.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from . import config

#: A tenant id is a conservative slug: lowercase alnum, ``-``/``_`` inside, leading
#: alphanumeric. This both names a directory and is part of every resolved path, so
#: it must never contain a separator or traversal token.
_TENANT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class TenancyError(Exception):
    """Base class for tenancy faults."""


class InvalidTenantId(TenancyError, ValueError):
    """A tenant id is malformed (not a safe slug)."""


class PathEscapeError(TenancyError, ValueError):
    """A resolved path would escape the tenant root (traversal / absolute escape)."""


class NoTenantContextError(TenancyError, RuntimeError):
    """The store was reached with no active TenantContext bound at the boundary."""


def validate_tenant_id(tenant_id: str) -> str:
    """Return ``tenant_id`` if it is a safe slug, else raise :class:`InvalidTenantId`."""
    if not isinstance(tenant_id, str) or tenant_id in {".", ".."} or not _TENANT_ID_RE.match(tenant_id):
        raise InvalidTenantId(
            f"invalid tenant id {tenant_id!r}: use lowercase [a-z0-9_-] with a leading alphanumeric"
        )
    return tenant_id


def _safe_segment(value: str, label: str) -> str:
    """A single path segment (a page id / source ref) with no separator or traversal."""
    if not isinstance(value, str) or "/" in value or "\\" in value or value in {"", ".", ".."}:
        raise PathEscapeError(f"unsafe {label}: {value!r}")
    return value


# --- Tenant model ----------------------------------------------------------


@dataclass(frozen=True)
class Tenant:
    """A tenant record (metadata only — its content lives under its own root)."""

    tenant_id: str
    name: str
    status: str = "active"  # active | suspended
    created: str = ""
    #: Default visibility applied to new pages ingested in this tenant (T4).
    default_visibility: str = "shared"  # shared | private
    #: Per-tenant resource quotas (T7); 0 = use the config default / unlimited.
    max_pages: int = 0
    max_bytes: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Tenant":
        tid = validate_tenant_id(d["tenant_id"])
        return cls(
            tenant_id=tid,
            name=d.get("name") or tid,
            status=d.get("status", "active"),
            created=d.get("created", ""),
            default_visibility=d.get("default_visibility", "shared"),
            max_pages=int(d.get("max_pages", 0) or 0),
            max_bytes=int(d.get("max_bytes", 0) or 0),
        )


# --- Tenant registry (metadata store, OUTSIDE any tenant root) -------------


class TenantRegistry:
    """A small JSON registry recording WHICH tenants exist, at
    ``DATA_ROOT/registry.json`` — beside, never inside, the tenant roots. It holds
    no tenant content."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else config.registry_path()

    def _load(self) -> dict[str, Tenant]:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except (ValueError, OSError):
            return {}
        out: dict[str, Tenant] = {}
        for tid, d in (data.get("tenants") or {}).items():
            try:
                out[tid] = Tenant.from_dict({"tenant_id": tid, **(d or {})})
            except (TenancyError, KeyError):
                continue
        return out

    def _save(self, tenants: dict[str, Tenant]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "tenants": {
                t.tenant_id: {
                    "name": t.name,
                    "status": t.status,
                    "created": t.created,
                    "default_visibility": t.default_visibility,
                    "max_pages": t.max_pages,
                    "max_bytes": t.max_bytes,
                }
                for t in tenants.values()
            }
        }
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    def exists(self, tenant_id: str) -> bool:
        return validate_tenant_id(tenant_id) in self._load()

    def get(self, tenant_id: str) -> Tenant | None:
        return self._load().get(validate_tenant_id(tenant_id))

    def list(self) -> list[Tenant]:
        return sorted(self._load().values(), key=lambda t: t.tenant_id)

    def ensure(self, tenant_id: str, name: str | None = None) -> Tenant:
        """Idempotently record a tenant; return the existing or newly-created one."""
        validate_tenant_id(tenant_id)
        tenants = self._load()
        existing = tenants.get(tenant_id)
        if existing is not None:
            return existing
        tenant = Tenant(tenant_id=tenant_id, name=name or tenant_id, status="active", created=config.now_iso())
        tenants[tenant_id] = tenant
        self._save(tenants)
        return tenant

    def set_default_visibility(self, tenant_id: str, visibility: str) -> Tenant:
        """Set a tenant's default new-page visibility (``shared``/``private``). (T4;
        the admin surface for it lands in T7.)"""
        tid = validate_tenant_id(tenant_id)
        tenants = self._load()
        if tid not in tenants:
            raise TenancyError(f"unknown tenant: {tid}")
        updated = replace(tenants[tid], default_visibility=visibility)
        tenants[tid] = updated
        self._save(tenants)
        return updated

    def set_status(self, tenant_id: str, status: str) -> Tenant:
        """Set a tenant's lifecycle status (``active``/``suspended``) — T7."""
        tid = validate_tenant_id(tenant_id)
        tenants = self._load()
        if tid not in tenants:
            raise TenancyError(f"unknown tenant: {tid}")
        updated = replace(tenants[tid], status=status)
        tenants[tid] = updated
        self._save(tenants)
        return updated

    def set_quota(self, tenant_id: str, *, max_pages: int | None = None, max_bytes: int | None = None) -> Tenant:
        """Set a tenant's per-tenant resource quotas (0 = unlimited) — T7."""
        tid = validate_tenant_id(tenant_id)
        tenants = self._load()
        if tid not in tenants:
            raise TenancyError(f"unknown tenant: {tid}")
        t = tenants[tid]
        updated = replace(
            t,
            max_pages=int(max_pages) if max_pages is not None else t.max_pages,
            max_bytes=int(max_bytes) if max_bytes is not None else t.max_bytes,
        )
        tenants[tid] = updated
        self._save(tenants)
        return updated

    def remove(self, tenant_id: str) -> bool:
        """Drop a tenant's registry record (used by lifecycle delete) — T7."""
        tid = validate_tenant_id(tenant_id)
        tenants = self._load()
        if tid not in tenants:
            return False
        del tenants[tid]
        self._save(tenants)
        return True


# --- TenantContext (resolved at boundaries) --------------------------------


@dataclass(frozen=True)
class TenantContext:
    """The isolation handle: a tenant id + its absolute physical root. Every store
    object is constructed from one of these, and every path is resolved against
    ``root_path`` with a containment guard."""

    tenant_id: str
    root_path: Path

    def __post_init__(self) -> None:
        validate_tenant_id(self.tenant_id)
        object.__setattr__(self, "root_path", Path(self.root_path).resolve())

    # canonical (tracked)
    @property
    def pages_dir(self) -> Path:
        return self.root_path / "pages"

    @property
    def sources_dir(self) -> Path:
        return self.root_path / "sources"

    # rebuildable caches (gitignored); each tenant root is its own git repo
    @property
    def cache_dir(self) -> Path:
        return self.root_path / ".cache"

    @property
    def git_root(self) -> Path:
        return self.root_path

    def resolve(self, *parts: str) -> Path:
        """Join ``parts`` under the tenant root and GUARD the result: it must stay
        inside the root. Traversal (``..``) and absolute escapes are refused."""
        candidate = (self.root_path / Path(*parts)).resolve()
        if candidate != self.root_path and not candidate.is_relative_to(self.root_path):
            raise PathEscapeError(
                f"path escapes tenant {self.tenant_id!r} root ({self.root_path}): {candidate}"
            )
        return candidate

    def page_path(self, page_id: str) -> Path:
        _safe_segment(page_id, "page id")
        return self.resolve("pages", f"{page_id}.md")

    def source_path(self, source_ref: str) -> Path:
        _safe_segment(source_ref, "source ref")
        return self.resolve("sources", f"{source_ref}.md")

    def cache_path(self, name: str) -> Path:
        _safe_segment(name, "cache file")
        return self.resolve(".cache", name)

    def ensure_dirs(self) -> None:
        for d in (self.root_path, self.pages_dir, self.sources_dir, self.cache_dir):
            d.mkdir(parents=True, exist_ok=True)


# --- Factories -------------------------------------------------------------


def context_for(tenant_id: str, *, data_root: Path | str | None = None) -> TenantContext:
    """Resolve a TenantContext for ``tenant_id`` (no side effects). ``data_root``
    overrides ``config.DATA_ROOT`` (used by tests)."""
    validate_tenant_id(tenant_id)
    base = Path(data_root) / config.TENANTS_DIRNAME if data_root is not None else config.tenants_dir()
    return TenantContext(tenant_id=tenant_id, root_path=base / tenant_id)


def default_context(*, data_root: Path | str | None = None) -> TenantContext:
    """The single-tenant deployment's ``default`` tenant context."""
    return context_for(config.DEFAULT_TENANT_ID, data_root=data_root)


def _ensure_identity(repo_root: Path) -> None:
    """Set a local PoC git identity only if none is configured (respects ~/.gitconfig)."""
    for key, value in (("user.name", "mnesis PoC"), ("user.email", "mnesis@localhost")):
        result = subprocess.run(
            ["git", "-C", str(repo_root), "config", key], capture_output=True, text=True
        )
        if result.returncode != 0 or not result.stdout.strip():
            subprocess.run(["git", "-C", str(repo_root), "config", key, value], check=True)


def _ensure_gitignore(ctx: TenantContext) -> None:
    """Keep the rebuildable cache out of the tenant's git history."""
    gi = ctx.root_path / ".gitignore"
    line = ".cache/"
    existing = gi.read_text(encoding="utf-8") if gi.is_file() else ""
    if line not in existing.splitlines():
        gi.write_text((existing.rstrip() + "\n" if existing else "") + line + "\n", encoding="utf-8")


def init_git(ctx: TenantContext) -> None:
    """Make the tenant root its own git repo (idempotent): init if needed, set a
    fallback identity, and ignore the cache dir."""
    ctx.ensure_dirs()
    if not (ctx.git_root / ".git").exists():
        subprocess.run(["git", "-C", str(ctx.git_root), "init", "-q"], check=True)
    _ensure_identity(ctx.git_root)
    _ensure_gitignore(ctx)


def create_tenant(
    tenant_id: str,
    name: str | None = None,
    *,
    registry: TenantRegistry | None = None,
    data_root: Path | str | None = None,
) -> TenantContext:
    """Provision a tenant idempotently: record it in the registry, create its dirs,
    and init its own git repo. Returns its TenantContext. Safe to call repeatedly."""
    (registry or TenantRegistry()).ensure(tenant_id, name)
    ctx = context_for(tenant_id, data_root=data_root)
    init_git(ctx)
    return ctx


def open_tenant(tenant_id: str, *, data_root: Path | str | None = None) -> TenantContext:
    """Resolve and provision a tenant for use at a boundary (CLI/HTTP/MCP).

    For the ``default`` tenant this also runs the idempotent legacy migration, so a
    single-tenant deployment is transparent — existing data is moved into
    ``tenants/default/`` on first use and run as the one default tenant thereafter.
    """
    if tenant_id == config.DEFAULT_TENANT_ID:
        migrate_legacy_to_default(data_root=data_root)
    return create_tenant(tenant_id, data_root=data_root)


# --- Active-context binding (resolved at boundaries; fail-closed) -----------

_active: ContextVar[TenantContext | None] = ContextVar("mnesis_active_tenant", default=None)


def current() -> TenantContext:
    """The active TenantContext, or raise :class:`NoTenantContextError` if none is
    bound. The store is unreachable without first resolving a tenant at a boundary."""
    ctx = _active.get()
    if ctx is None:
        raise NoTenantContextError(
            "no active TenantContext: a tenant must be resolved and bound at the boundary "
            "(e.g. `with mnesis.tenancy.use(ctx): ...`) before the store can be reached"
        )
    return ctx


def current_or_none() -> TenantContext | None:
    return _active.get()


def bind(ctx: TenantContext) -> Token:
    """Bind ``ctx`` as the active tenant; returns a token for :func:`unbind`."""
    if not isinstance(ctx, TenantContext):
        raise TypeError(f"bind() needs a TenantContext, got {type(ctx).__name__}")
    return _active.set(ctx)


def unbind(token: Token) -> None:
    _active.reset(token)


@contextmanager
def use(ctx: TenantContext):
    """Bind ``ctx`` for the duration of the ``with`` block (the boundary pattern)."""
    token = bind(ctx)
    try:
        yield ctx
    finally:
        unbind(token)


# --- Migration: legacy single-store layout -> tenants/default/ -------------


def _legacy_layout_present(data_root: Path) -> bool:
    return (data_root / "pages").is_dir() or (data_root / "sources").is_dir()


def migrate_legacy_to_default(
    *,
    data_root: Path | str | None = None,
    registry: TenantRegistry | None = None,
) -> dict:
    """Move an existing single-store layout (``DATA_ROOT/{pages,sources}``) into
    ``tenants/default/`` and give it its own git repo. **Non-destructive** (content
    is moved, never dropped; the legacy ``.git``/``.index`` are left in place) and
    **idempotent** (a re-run, once ``tenants/default/`` exists, is a no-op).

    Returns ``{"migrated": bool, "tenant": "default", "moved": [...], "pages": N}``.
    """
    root = Path(data_root).resolve() if data_root is not None else config.DATA_ROOT
    reg = registry or TenantRegistry(root / config.REGISTRY_FILENAME)
    ctx = context_for(config.DEFAULT_TENANT_ID, data_root=root)

    already = ctx.pages_dir.exists() or ctx.sources_dir.exists()
    moved: list[str] = []
    if not already and _legacy_layout_present(root):
        ctx.root_path.mkdir(parents=True, exist_ok=True)
        for sub in ("pages", "sources"):
            src = root / sub
            if src.is_dir():
                shutil.move(str(src), str(ctx.root_path / sub))
                moved.append(sub)

    # Provision (idempotent): registry record + dirs + own git repo, then commit
    # any migrated canonical content as the tenant's first commit.
    reg.ensure(config.DEFAULT_TENANT_ID)
    init_git(ctx)
    if moved:
        subprocess.run(["git", "-C", str(ctx.git_root), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(ctx.git_root), "commit", "-q", "--allow-empty",
             "-m", "mnesis: migrate existing data into the default tenant"],
            check=True,
        )

    pages = len(list(ctx.pages_dir.glob("*.md"))) if ctx.pages_dir.exists() else 0
    return {"migrated": bool(moved), "tenant": config.DEFAULT_TENANT_ID, "moved": moved, "pages": pages}
