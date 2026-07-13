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

from . import config, vocab

#: A tenant id is a conservative slug: lowercase alnum, ``-``/``_`` inside, leading
#: alphanumeric. This both names a directory and is part of every resolved path, so
#: it must never contain a separator or traversal token.
_TENANT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

#: A vault id follows exactly the same safe-slug rule as a tenant id (it, too, names a
#: directory segment inside a resolved path).
_VAULT_ID_RE = _TENANT_ID_RE


class TenancyError(Exception):
    """Base class for tenancy faults."""


class InvalidTenantId(TenancyError, ValueError):
    """A tenant id is malformed (not a safe slug)."""


class InvalidVaultId(TenancyError, ValueError):
    """A vault id is malformed (not a safe slug)."""


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


def validate_vault_id(vault_id: str) -> str:
    """Return ``vault_id`` if it is a safe slug, else raise :class:`InvalidVaultId`."""
    if not isinstance(vault_id, str) or vault_id in {".", ".."} or not _VAULT_ID_RE.match(vault_id):
        raise InvalidVaultId(
            f"invalid vault id {vault_id!r}: use lowercase [a-z0-9_-] with a leading alphanumeric"
        )
    return vault_id


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


# --- Vault model (a per-user, in-tenant isolation unit) --------------------


@dataclass(frozen=True)
class Vault:
    """A vault record (metadata only — its content lives under its own vault root).

    A vault is the per-user, in-tenant isolation unit: a full canonical store (its own
    ``pages/``/``sources/``/``.cache/`` + git repo) nested under one tenant. A vault
    always belongs to exactly one tenant; tenant isolation is unchanged and unweakened.
    """

    vault_id: str
    tenant_id: str
    name: str = ""
    owner_principal: str | None = None
    status: str = "active"  # active | suspended
    created: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Vault":
        vid = validate_vault_id(d["vault_id"])
        return cls(
            vault_id=vid,
            tenant_id=validate_tenant_id(d["tenant_id"]),
            name=d.get("name") or vid,
            owner_principal=d.get("owner_principal"),
            status=d.get("status", "active"),
            created=d.get("created", ""),
        )


# --- Vault registry (per-tenant metadata, OUTSIDE any vault root) ----------


class VaultRegistry:
    """A small JSON registry recording, for one tenant, WHICH vaults exist **and who may
    access them** — at ``tenants/<tenant_id>/vaults.json`` (at the tenant root, beside,
    never inside, the vault roots). It holds no vault content.

    The document has two parts: ``vaults`` (the :class:`Vault` records) and ``grants``
    (``{principal_id: {vault_id: role|null}}`` — an explicit access grant per principal
    per vault, with an optional per-vault role that *narrows/overrides* the principal's
    tenant role; ``null`` means "use the tenant role"). Access to a vault = **being its
    owner OR holding an explicit grant** (the transparent ``default`` vault is handled by
    the resolver, not here). This is the store of record for vault access control (V2)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    # -- unified doc read/write (preserves both `vaults` and `grants`) ---------
    def _read(self) -> dict:
        if not self.path.is_file():
            return {"vaults": {}, "grants": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except (ValueError, OSError):
            return {"vaults": {}, "grants": {}}
        if not isinstance(data, dict):
            return {"vaults": {}, "grants": {}}
        data.setdefault("vaults", {})
        data.setdefault("grants", {})
        return data

    def _write(self, doc: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    def _load(self) -> dict[str, Vault]:
        out: dict[str, Vault] = {}
        for vid, d in (self._read().get("vaults") or {}).items():
            try:
                out[vid] = Vault.from_dict({"vault_id": vid, **(d or {})})
            except (TenancyError, KeyError):
                continue
        return out

    def _save_vaults(self, vaults: dict[str, Vault]) -> None:
        doc = self._read()
        doc["vaults"] = {
            v.vault_id: {
                "tenant_id": v.tenant_id,
                "name": v.name,
                "owner_principal": v.owner_principal,
                "status": v.status,
                "created": v.created,
            }
            for v in vaults.values()
        }
        self._write(doc)

    # -- vaults ----------------------------------------------------------------
    def exists(self, vault_id: str) -> bool:
        return validate_vault_id(vault_id) in self._load()

    def get(self, vault_id: str) -> Vault | None:
        return self._load().get(validate_vault_id(vault_id))

    def list(self) -> list[Vault]:
        return sorted(self._load().values(), key=lambda v: v.vault_id)

    def ensure(
        self, vault_id: str, *, tenant_id: str, name: str | None = None,
        owner_principal: str | None = None,
    ) -> Vault:
        """Idempotently record a vault; return the existing or newly-created one."""
        validate_vault_id(vault_id)
        vaults = self._load()
        existing = vaults.get(vault_id)
        if existing is not None:
            return existing
        vault = Vault(
            vault_id=vault_id, tenant_id=validate_tenant_id(tenant_id),
            name=name or vault_id, owner_principal=owner_principal,
            status="active", created=config.now_iso(),
        )
        vaults[vault_id] = vault
        self._save_vaults(vaults)
        return vault

    def remove(self, vault_id: str) -> bool:
        vid = validate_vault_id(vault_id)
        vaults = self._load()
        if vid not in vaults:
            return False
        del vaults[vid]
        self._save_vaults(vaults)
        # Drop any dangling grants to the removed vault.
        doc = self._read()
        grants = doc.get("grants") or {}
        for pid in list(grants):
            grants[pid].pop(vid, None)
            if not grants[pid]:
                del grants[pid]
        doc["grants"] = grants
        self._write(doc)
        return True

    # -- grants (vault access control, V2) -------------------------------------
    def grant(self, principal_id: str, vault_id: str, role: str | None = None) -> None:
        """Grant ``principal_id`` access to ``vault_id`` (idempotent; ``role`` optionally
        narrows/overrides the principal's tenant role for this vault). The vault must
        exist in this tenant's registry (a grant to an unknown vault is refused)."""
        vid = validate_vault_id(vault_id)
        if vid not in self._load():
            raise TenancyError(f"cannot grant access to unknown vault {vid!r}")
        doc = self._read()
        grants = doc.setdefault("grants", {})
        grants.setdefault(principal_id, {})[vid] = role
        self._write(doc)

    def revoke_grant(self, principal_id: str, vault_id: str) -> bool:
        """Revoke ``principal_id``'s explicit grant to ``vault_id`` (ownership is not a
        grant and is unaffected). Returns whether a grant was removed."""
        vid = validate_vault_id(vault_id)
        doc = self._read()
        grants = doc.get("grants") or {}
        pg = grants.get(principal_id)
        if not pg or vid not in pg:
            return False
        del pg[vid]
        if not pg:
            del grants[principal_id]
        doc["grants"] = grants
        self._write(doc)
        return True

    def grants_for(self, principal_id: str) -> dict[str, str | None]:
        """The explicit ``{vault_id: role|None}`` grants held by ``principal_id`` (no
        ownership). A copy — mutating it does not touch the registry."""
        return dict((self._read().get("grants") or {}).get(principal_id, {}))

    def owned_vaults(self, principal_id: str) -> set[str]:
        return {v.vault_id for v in self._load().values() if v.owner_principal == principal_id}

    def accessible_vaults(self, principal_id: str) -> set[str]:
        """Every vault ``principal_id`` may reach here = owned ∪ explicitly granted."""
        return self.owned_vaults(principal_id) | set(self.grants_for(principal_id))

    def has_access(self, principal_id: str, vault_id: str, *, owner_ok: bool = True) -> bool:
        """Whether ``principal_id`` owns or is granted ``vault_id`` (does NOT special-case
        the ``default`` vault — that is the resolver's transparent-single-vault rule)."""
        vid = validate_vault_id(vault_id)
        if owner_ok and vid in self.owned_vaults(principal_id):
            return True
        return vid in self.grants_for(principal_id)

    def role_for(self, principal_id: str, vault_id: str, default_role: str) -> str:
        """The principal's effective role *in* ``vault_id``: an explicit per-vault grant
        role if set, else ``default_role`` (the principal's tenant role)."""
        role = self.grants_for(principal_id).get(validate_vault_id(vault_id))
        return role or default_role


# --- TenantContext + VaultContext (resolved at boundaries) -----------------


@dataclass(frozen=True)
class TenantContext:
    """A **tenant-level** handle: a tenant id + its absolute tenant root
    (``tenants/<tenant_id>/``). It is **not** a store — a store is reached only through
    a :class:`VaultContext` (below). This handle owns tenant-level metadata: the vault
    registry and the enumeration/opening of the tenant's vaults.

    The path helpers (``pages_dir``/``resolve``/…) operate on ``root_path`` and are the
    shared implementation :class:`VaultContext` reuses (there ``root_path`` is the vault
    root); on a bare tenant handle they describe the tenant root and are not used as a
    store (:class:`~mnesis.store.Store` rejects anything that is not a VaultContext)."""

    tenant_id: str
    root_path: Path

    def __post_init__(self) -> None:
        validate_tenant_id(self.tenant_id)
        object.__setattr__(self, "root_path", Path(self.root_path).resolve())

    # -- tenant-level: the vaults live under the TENANT root --------------------
    @property
    def tenant_root(self) -> Path:
        """The tenant root (``tenants/<tenant_id>/``) — where vaults + the vault
        registry live. For a bare TenantContext this is ``root_path``; a VaultContext
        overrides it (its ``root_path`` is the vault root)."""
        return self.root_path

    @property
    def vaults_dir(self) -> Path:
        return self.tenant_root / config.VAULTS_DIRNAME

    @property
    def vault_registry_path(self) -> Path:
        return self.tenant_root / config.VAULT_REGISTRY_FILENAME

    def vault_registry(self) -> "VaultRegistry":
        return VaultRegistry(self.vault_registry_path)

    def vault_context(self, vault_id: str) -> "VaultContext":
        """The :class:`VaultContext` for ``vault_id`` under this tenant (no side effects)."""
        validate_vault_id(vault_id)
        return VaultContext(
            tenant_id=self.tenant_id,
            root_path=self.vaults_dir / vault_id,
            vault_id=vault_id,
            tenant_root_path=self.tenant_root,
        )

    # canonical (tracked)
    @property
    def pages_dir(self) -> Path:
        return self.root_path / "pages"

    @property
    def sources_dir(self) -> Path:
        return self.root_path / "sources"

    # rebuildable caches (gitignored); each vault root is its own git repo
    @property
    def cache_dir(self) -> Path:
        return self.root_path / ".cache"

    @property
    def git_root(self) -> Path:
        return self.root_path

    def resolve(self, *parts: str) -> Path:
        """Join ``parts`` under ``root_path`` and GUARD the result: it must stay inside
        the root. Traversal (``..``) and absolute escapes are refused. For a
        :class:`VaultContext` the root is the vault root, so a resolved path can never
        escape the vault."""
        candidate = (self.root_path / Path(*parts)).resolve()
        if candidate != self.root_path and not candidate.is_relative_to(self.root_path):
            raise PathEscapeError(
                f"path escapes {self.root_path}: {candidate}"
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

    @property
    def config_path(self) -> Path:
        """The per-vault schema/config file (``<vault_root>/config.json``, V3)."""
        return self.root_path / config.VAULT_CONFIG_FILENAME

    def ensure_dirs(self) -> None:
        for d in (self.root_path, self.pages_dir, self.sources_dir, self.cache_dir):
            d.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class VaultContext(TenantContext):
    """The isolation handle a **store** is constructed from: a (tenant, vault) pair and
    the absolute **vault root** (``tenants/<tenant_id>/vaults/<vault_id>/``). Every store
    object (:class:`~mnesis.store.Store`, the search index, the state store, the graph
    backend) is built from one of these, and every path is resolved against the vault
    root with a containment guard — so a resolved path can never escape the vault, and a
    store cannot be reached without a VaultContext.

    ``root_path`` is the vault root (the inherited store path helpers use it);
    ``tenant_root_path`` is the enclosing tenant root, where the vault registry lives."""

    vault_id: str = config.DEFAULT_VAULT_ID
    tenant_root_path: Path | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        validate_vault_id(self.vault_id)
        troot = self.tenant_root_path if self.tenant_root_path is not None else self.root_path.parent.parent
        object.__setattr__(self, "tenant_root_path", Path(troot).resolve())

    @property
    def tenant_root(self) -> Path:  # override: root_path is the vault root, not the tenant root
        return self.tenant_root_path


# --- Factories -------------------------------------------------------------


def tenant_context_for(tenant_id: str, *, data_root: Path | str | None = None) -> TenantContext:
    """Resolve a **tenant-level** :class:`TenantContext` (root = ``tenants/<id>/``) with
    no side effects. Used for tenant-level ops (vault registry, lifecycle); a store needs
    a :class:`VaultContext` from :func:`context_for`."""
    validate_tenant_id(tenant_id)
    base = Path(data_root) / config.TENANTS_DIRNAME if data_root is not None else config.tenants_dir()
    return TenantContext(tenant_id=tenant_id, root_path=base / tenant_id)


def context_for(
    tenant_id: str, vault_id: str | None = None, *, data_root: Path | str | None = None
) -> VaultContext:
    """Resolve a :class:`VaultContext` for ``tenant_id``/``vault_id`` (no side effects).

    ``vault_id`` defaults to the transparent ``default`` vault when omitted (``None``),
    so a single-vault deployment reaches its data with no vault argument; an explicit
    (even empty) vault id is validated. ``data_root`` overrides ``config.DATA_ROOT``."""
    vid = config.DEFAULT_VAULT_ID if vault_id is None else vault_id
    return tenant_context_for(tenant_id, data_root=data_root).vault_context(vid)


def default_context(*, data_root: Path | str | None = None) -> VaultContext:
    """The single-tenant/single-vault deployment's ``default`` tenant + ``default`` vault."""
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
) -> VaultContext:
    """Provision a tenant idempotently and return its **default vault**'s
    :class:`VaultContext`. Records the tenant in the registry, migrates any pre-vault
    tenant-root store into the default vault, provisions the vault (dirs + its own git
    repo) and records it in the tenant's vault registry. Safe to call repeatedly."""
    (registry or TenantRegistry()).ensure(tenant_id, name)
    # A pre-vault tenant store (tenants/<id>/{pages,sources}) is moved into the default
    # vault before the vault dirs are provisioned, so no data is stranded.
    migrate_tenant_to_default_vault(tenant_id, data_root=data_root)
    return context_for(tenant_id, config.DEFAULT_VAULT_ID, data_root=data_root)


def create_vault(
    tenant_id: str,
    vault_id: str,
    *,
    name: str | None = None,
    owner_principal: str | None = None,
    data_root: Path | str | None = None,
) -> VaultContext:
    """Provision an additional vault under an existing tenant idempotently: record it in
    the tenant's vault registry, create its dirs + its own git repo, and write its DEFAULT
    schema config (V3). Returns the vault's :class:`VaultContext`."""
    tctx = tenant_context_for(tenant_id, data_root=data_root)
    ctx = tctx.vault_context(vault_id)
    init_git(ctx)
    tctx.vault_registry().ensure(
        vault_id, tenant_id=tenant_id, name=name, owner_principal=owner_principal
    )
    vocab.ensure_config(ctx)  # default schema = the current global schema
    return ctx


def list_vaults(tenant_id: str, *, data_root: Path | str | None = None) -> list[Vault]:
    """The vaults recorded for ``tenant_id`` (metadata only)."""
    return tenant_context_for(tenant_id, data_root=data_root).vault_registry().list()


def open_tenant(tenant_id: str, *, data_root: Path | str | None = None) -> VaultContext:
    """Resolve and provision a tenant for use at a boundary (CLI/HTTP/MCP), returning its
    **default vault**'s :class:`VaultContext`.

    For the ``default`` tenant this also runs the idempotent legacy migration, so a
    single-tenant/single-vault deployment is transparent — existing data is moved into
    ``tenants/default/vaults/default/`` on first use and run as the one default vault
    thereafter.
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


_RESERVED_PAGE_FILES = {"index.md", "log.md"}  # OKF reserved files are not concept pages


def _legacy_layout_present(data_root: Path) -> bool:
    return (data_root / "pages").is_dir() or (data_root / "sources").is_dir()


def _count_pages(ctx: "TenantContext") -> int:
    if not ctx.pages_dir.exists():
        return 0
    return len([p for p in ctx.pages_dir.glob("*.md") if p.name not in _RESERVED_PAGE_FILES])


def migrate_tenant_to_default_vault(
    tenant_id: str, *, data_root: Path | str | None = None
) -> dict:
    """Move a **pre-vault** per-tenant store (``tenants/<id>/{pages,sources}``) into that
    tenant's ``default`` vault (``tenants/<id>/vaults/default/``) and give the vault its
    own git repo. **Non-destructive** (content is moved, never dropped; the tenant root's
    old ``.git``/``.index`` are left in place) and **idempotent** (once the default vault
    is populated a re-run is a no-op — no move, no commit). Always provisions the vault
    (dirs + git) and records it in the tenant's vault registry.

    Returns ``{"migrated": bool, "tenant": id, "vault": "default", "moved": [...], "pages": N}``.
    """
    tctx = tenant_context_for(tenant_id, data_root=data_root)
    vctx = tctx.vault_context(config.DEFAULT_VAULT_ID)

    vault_populated = vctx.pages_dir.exists() or vctx.sources_dir.exists()
    moved: list[str] = []
    if not vault_populated:
        for sub in ("pages", "sources"):
            src = tctx.root_path / sub
            if src.is_dir():
                vctx.root_path.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(vctx.root_path / sub))
                moved.append(sub)

    # Provision the vault (idempotent): dirs + its own git repo + registry record.
    init_git(vctx)
    tctx.vault_registry().ensure(config.DEFAULT_VAULT_ID, tenant_id=tenant_id)
    vocab.ensure_config(vctx)  # migrated/new default vault carries the default schema (V3)
    if moved:
        subprocess.run(["git", "-C", str(vctx.git_root), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(vctx.git_root), "commit", "-q", "--allow-empty",
             "-m", "mnesis: migrate existing data into the default vault"],
            check=True,
        )
    return {
        "migrated": bool(moved), "tenant": tenant_id, "vault": config.DEFAULT_VAULT_ID,
        "moved": moved, "pages": _count_pages(vctx),
    }


def migrate_legacy_to_default(
    *,
    data_root: Path | str | None = None,
    registry: TenantRegistry | None = None,
) -> dict:
    """Move an existing single-store layout (``DATA_ROOT/{pages,sources}``) into the
    ``default`` tenant's ``default`` vault (``tenants/default/vaults/default/``) and give
    the vault its own git repo. **Non-destructive** (content is moved, never dropped; the
    legacy ``.git``/``.index`` are left in place) and **idempotent** (a re-run, once the
    default vault exists, is a no-op).

    Legacy content is first staged at the tenant root, then handed to
    :func:`migrate_tenant_to_default_vault`, so both the pre-tenant (``DATA_ROOT/…``) and
    pre-vault (``tenants/default/…``) layouts converge on the default vault.

    Returns ``{"migrated": bool, "tenant": "default", "vault": "default", "moved": [...], "pages": N}``.
    """
    root = Path(data_root).resolve() if data_root is not None else config.DATA_ROOT
    reg = registry or TenantRegistry(root / config.REGISTRY_FILENAME)
    tctx = tenant_context_for(config.DEFAULT_TENANT_ID, data_root=root)
    vctx = tctx.vault_context(config.DEFAULT_VAULT_ID)

    vault_populated = vctx.pages_dir.exists() or vctx.sources_dir.exists()
    tenant_store_present = (tctx.root_path / "pages").is_dir() or (tctx.root_path / "sources").is_dir()
    moved: list[str] = []
    # Step 1: legacy single-store DATA_ROOT/{pages,sources} -> tenant root (staging).
    if not vault_populated and not tenant_store_present and _legacy_layout_present(root):
        tctx.root_path.mkdir(parents=True, exist_ok=True)
        for sub in ("pages", "sources"):
            src = root / sub
            if src.is_dir():
                shutil.move(str(src), str(tctx.root_path / sub))
                moved.append(sub)

    reg.ensure(config.DEFAULT_TENANT_ID)
    # Step 2: tenant-root store -> the default vault (+ its own git repo, first commit).
    vmig = migrate_tenant_to_default_vault(config.DEFAULT_TENANT_ID, data_root=root)

    return {
        "migrated": bool(moved or vmig["moved"]),
        "tenant": config.DEFAULT_TENANT_ID,
        "vault": config.DEFAULT_VAULT_ID,
        "moved": moved,
        "pages": vmig["pages"],
    }
