"""Open Knowledge Format (OKF) v0.1 — conformance contract + validator (OKF1).

Mnesis adopts Google Cloud's **Open Knowledge Format v0.1** as the interchange contract
for its canonical Markdown entries (see `docs/OKF.md` for the grounded reference and the
full field-mapping table). This module captures the contract as constants and a
**validator**, and provides the Mnesis→OKF reconciliation as pure functions. It does
**not** rewrite any page — the store's on-disk format is migrated in a later step.

The spec (paraphrased, grounded in `okf/SPEC.md`):

  - **Required:** ``type`` — a short, non-empty string naming the kind of concept
    (not centrally registered; consumers tolerate unknown values).
  - **Recommended:** ``title``, ``description``, ``resource`` (a URI for the underlying
    asset; omitted for abstract concepts), ``tags`` (list of strings), ``timestamp``
    (ISO 8601 of the last meaningful change). Producers MAY add arbitrary fields;
    consumers SHOULD preserve unknown keys when round-tripping.
  - **Conformance (bundle):** every non-reserved ``.md`` file has *parseable* YAML
    frontmatter containing a non-empty ``type``; reserved files follow their structure.
  - **Consumers MUST NOT reject** for: missing optional fields · unknown ``type`` values
    · unknown extra keys · broken cross-links · missing ``index.md``.
  - **Identity:** a concept's id is its bundle-relative path with ``.md`` removed
    (``tables/users.md`` → ``tables/users``).
  - **Cross-links:** ordinary Markdown links; **bundle-absolute** (begin with ``/``,
    relative to the bundle root) are recommended (stable when files move). A link
    asserts a *relationship*; its kind is conveyed by prose, not by the link.
  - **Reserved files:** ``index.md`` (directory listing; **no frontmatter permitted**)
    and ``log.md`` (update history; ISO 8601 date headings + prose).

Where the spec is **strict** (enforced as *errors* here): frontmatter must parse, ``type``
must be present + non-empty, reserved-file formats. Where it is **lenient** (surfaced as
*warnings* or ignored): missing recommended fields, unknown types/keys, broken links,
unknown headings. `Mnesis extension fields` (``kind``/``status``/``source_count``/… — see
:func:`to_okf_metadata`) are exactly such tolerated extra keys and never break conformance.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import frontmatter

OKF_VERSION = "0.1"

#: The one required OKF field.
OKF_REQUIRED: tuple[str, ...] = ("type",)
#: OKF's recommended fields (all optional; consumers must not reject on absence).
OKF_RECOMMENDED: tuple[str, ...] = ("title", "description", "resource", "tags", "timestamp")
#: The field set the OKF **reference parser** reads — target these for interop robustness.
REFERENCE_PARSER_FIELDS: tuple[str, ...] = ("type", "title", "description", "timestamp")
#: Reserved filenames with mandated structure.
RESERVED_FILES: tuple[str, ...] = ("index.md", "log.md")

_FRONTMATTER_FENCE = re.compile(r"^﻿?\s*---\s*(?:\n|\r\n)")
_MD_LINK = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
#: A reasonable ISO-8601-date heading for log.md (``## 2026-06-10`` or a full datetime).
_LOG_DATE_HEADING = re.compile(r"^#{1,6}\s+(\d{4}-\d{2}-\d{2}(?:[T ][\d:.,+Zz-]+)?)\s*$")


# --- issues + report -------------------------------------------------------


@dataclass(frozen=True)
class OKFIssue:
    """A single conformance finding. ``level`` is ``"error"`` (breaks conformance) or
    ``"warning"`` (interop advice — never breaks conformance)."""

    level: str
    code: str
    message: str
    path: str | None = None
    field: str | None = None

    def __str__(self) -> str:
        where = f"{self.path}: " if self.path else ""
        fld = f" [{self.field}]" if self.field else ""
        return f"{where}{self.level}: {self.message}{fld} ({self.code})"


@dataclass
class OKFReport:
    """The outcome of validating a document or a bundle. ``conformant`` is True iff
    there are no ``error``-level issues (warnings do not break conformance)."""

    issues: list[OKFIssue] = field(default_factory=list)
    documents: int = 0  # non-reserved concept docs checked

    @property
    def errors(self) -> list[OKFIssue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[OKFIssue]:
        return [i for i in self.issues if i.level == "warning"]

    @property
    def conformant(self) -> bool:
        return not self.errors

    def __bool__(self) -> bool:
        return self.conformant

    def add(self, level: str, code: str, message: str, *, path: str | None = None, field: str | None = None) -> None:
        self.issues.append(OKFIssue(level, code, message, path=path, field=field))

    def extend(self, other: "OKFReport") -> None:
        self.issues.extend(other.issues)
        self.documents += other.documents


# --- helpers ----------------------------------------------------------------


def _has_frontmatter(text: str) -> bool:
    return bool(_FRONTMATTER_FENCE.match(text or ""))


def _is_iso8601(value) -> bool:
    if not isinstance(value, (str, datetime)):
        return False
    if isinstance(value, datetime):
        return True
    try:
        datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return True
    except (ValueError, TypeError):
        return False


def concept_id(path: str | Path, bundle_root: str | Path | None = None) -> str:
    """The OKF concept id for a file: its bundle-relative path with ``.md`` removed.
    ``tables/users.md`` → ``tables/users`` (POSIX separators, spec-style)."""
    p = Path(path)
    if bundle_root is not None:
        try:
            p = p.relative_to(bundle_root)
        except ValueError:
            pass
    s = p.as_posix()
    return s[:-3] if s.endswith(".md") else s


def cross_links(body: str) -> list[str]:
    """Every Markdown link target in ``body`` (bundle-absolute ``/…``, relative, or
    external). OKF conveys relationships as links; the kind is in the prose."""
    return _MD_LINK.findall(body or "")


# --- document validation ----------------------------------------------------


def validate_document(text: str, *, path: str | None = None) -> OKFReport:
    """Validate a single OKF document (Markdown + frontmatter) against v0.1.

    If ``path`` names a reserved file (``index.md``/``log.md``), the reserved-file rules
    apply instead of the concept rules. Errors: unparseable frontmatter, missing/empty
    ``type``, a reserved-file structure violation. Warnings: missing reference-parser
    fields (``title``/``description``/``timestamp``) and a non-ISO ``timestamp``."""
    report = OKFReport()
    name = Path(path).name if path else None

    if name in RESERVED_FILES:
        _validate_reserved(text, name, path, report)
        return report

    report.documents = 1
    # 1) Frontmatter must be present and parse (strict).
    try:
        post = frontmatter.loads(text or "")
    except Exception as exc:  # noqa: BLE001 — any YAML error is a hard non-conformance
        report.add("error", "unparseable_frontmatter",
                   f"frontmatter is not parseable YAML: {exc}", path=path)
        return report
    if not _has_frontmatter(text):
        report.add("error", "no_frontmatter",
                   "no YAML frontmatter block (OKF requires frontmatter with a `type`)", path=path)
        return report
    meta = post.metadata or {}

    # 2) `type` must be present and non-empty (strict).
    tval = meta.get("type")
    if tval is None or (isinstance(tval, str) and not tval.strip()) or tval == "":
        report.add("error", "missing_type", "required field `type` is missing or empty",
                   path=path, field="type")
    elif not isinstance(tval, str):
        report.add("warning", "type_not_string",
                   f"`type` should be a string, got {type(tval).__name__}", path=path, field="type")

    # 3) Reference-parser interop (lenient — warnings only).
    for f in ("title", "description", "timestamp"):
        v = meta.get(f)
        if v is None or (isinstance(v, str) and not v.strip()):
            report.add("warning", "missing_recommended",
                       f"recommended field `{f}` is absent (the OKF reference parser reads it)",
                       path=path, field=f)
    if meta.get("timestamp") is not None and not _is_iso8601(meta.get("timestamp")):
        report.add("warning", "timestamp_not_iso8601",
                   "`timestamp` should be ISO 8601", path=path, field="timestamp")

    # Unknown/extra keys are tolerated by construction (no issue) — Mnesis extensions live here.
    return report


def _validate_reserved(text: str, name: str, path: str | None, report: OKFReport) -> None:
    """Reserved-file structure (strict): ``index.md`` has NO frontmatter; ``log.md``
    uses ISO 8601 date headings."""
    if name == "index.md":
        if _has_frontmatter(text):
            report.add("error", "index_has_frontmatter",
                       "index.md must not contain frontmatter", path=path)
        return
    if name == "log.md":
        headings = [m.group(0) for m in re.finditer(r"^#{1,6}\s+.*$", text or "", re.MULTILINE)]
        dated = [h for h in headings if _LOG_DATE_HEADING.match(h)]
        if headings and not dated:
            # spec is prose-lenient about entries; flag only if NO heading is an ISO date
            report.add("warning", "log_headings_not_iso",
                       "log.md headings should be ISO 8601 dates", path=path)


# --- bundle validation ------------------------------------------------------


def validate_bundle(root: str | Path) -> OKFReport:
    """Validate an OKF **bundle** (a directory of ``.md`` concept files) against v0.1.

    Walks ``root`` recursively: reserved files (``index.md``/``log.md``) get the
    reserved rules; every other ``.md`` is a concept doc. A missing ``index.md`` is
    **not** an error (consumers must tolerate it). ``conformant`` is True iff no ``.md``
    file produced an error."""
    root = Path(root)
    report = OKFReport()
    if not root.is_dir():
        report.add("error", "no_bundle", f"bundle root is not a directory: {root}", path=str(root))
        return report
    for md in sorted(root.rglob("*.md")):
        rel = md.relative_to(root).as_posix()
        try:
            text = md.read_text(encoding="utf-8")
        except OSError as exc:
            report.add("error", "unreadable", f"cannot read file: {exc}", path=rel)
            continue
        report.extend(validate_document(text, path=rel))
    return report


# --- Mnesis → OKF reconciliation (the mapping made executable) --------------
# The target mapping (see docs/OKF.md). Used by the later migration + by tests to prove
# the extensions are tolerated. The store is NOT changed here.


def _first_sentence(body: str, limit: int = 200) -> str:
    """A one-sentence description from a page body (skips a trailing ``Source:`` line)."""
    for para in (strip_generated_links(body) or "").split("\n\n"):
        p = " ".join(para.split())
        if p and not p.lower().startswith("source:") and not p.startswith("#"):
            m = re.match(r"(.+?[.!?])(\s|$)", p)
            return (m.group(1) if m else p)[:limit]
    return ""


# --- OKF cross-links in the body (generated from relations + lifecycle) -----
# The links are marker-fenced so a written body round-trips to the clean prose:
# strip_generated_links() removes the block, render_okf_body() regenerates it.

_LINKS_BEGIN = "<!-- okf:links -->"
_LINKS_END = "<!-- /okf:links -->"
_LINKS_BLOCK = re.compile(r"\n*" + re.escape(_LINKS_BEGIN) + r".*?" + re.escape(_LINKS_END) + r"\n*", re.DOTALL)


def strip_generated_links(body: str) -> str:
    """Recover the clean human prose from a body that may carry a generated OKF
    cross-links block (idempotent — safe on a body that has none)."""
    return _LINKS_BLOCK.sub("", body or "").rstrip()


def _entity_concept_path(ref: str) -> str:
    """A bundle-absolute concept path for a ``type:value`` entity ref (`library:redis`
    → `/library/redis`) or a plain page id (`atlas` → `/atlas`)."""
    ref = (ref or "").strip()
    if ":" in ref:
        t, v = ref.split(":", 1)
        return f"/{t}/{v}"
    return f"/{ref}"


def cross_link_lines(page) -> list[str]:
    """OKF cross-link bullets for a page: one per typed relation (linking both
    endpoints, the predicate carried as prose — OKF conveys the relationship *kind* in
    prose, not the link) plus the lifecycle links (supersedes / superseded_by /
    contradicts). Bundle-absolute Markdown links throughout."""
    lines: list[str] = []
    seen: set = set()
    for rel in getattr(page, "relations", None) or []:
        s, p, o = rel.get("s"), rel.get("p"), rel.get("o")
        if not (s and p and o) or (s, p, o) in seen:
            continue
        seen.add((s, p, o))
        lines.append(f"- [{s}]({_entity_concept_path(s)}) *{p}* [{o}]({_entity_concept_path(o)})")
    if getattr(page, "supersedes", None):
        lines.append(f"- *supersedes* [{page.supersedes}](/{page.supersedes})")
    if getattr(page, "superseded_by", None):
        lines.append(f"- *superseded by* [{page.superseded_by}](/{page.superseded_by})")
    for c in getattr(page, "contradicts", None) or []:
        lines.append(f"- *contradicts* [{c}](/{c})")
    return lines


def render_okf_body(page) -> str:
    """The clean page prose plus a generated, marker-fenced **OKF cross-links** section
    (from the page's relations + lifecycle links). Round-trips: `strip_generated_links`
    on the result recovers the clean prose."""
    clean = strip_generated_links((getattr(page, "body", "") or "").strip())
    lines = cross_link_lines(page)
    if not lines:
        return clean
    block = f"{_LINKS_BEGIN}\n## Related\n\n" + "\n".join(lines) + f"\n{_LINKS_END}"
    return f"{clean}\n\n{block}" if clean else block


#: Mnesis fields that ride along as OKF-tolerated **extension** keys (never OKF-core).
MNESIS_EXTENSION_FIELDS: tuple[str, ...] = (
    "id", "kind", "status", "created", "sources", "source_count", "last_confirmed",
    "supersedes", "superseded_by", "contradicts", "decay_class", "relations",
    "owner_principal", "visibility", "question",
)


def to_okf_metadata(page) -> dict:
    """Map a :class:`mnesis.store.Page` to **OKF-conformant** frontmatter: the OKF core
    fields (``type`` from the page ``kind``; ``title``; ``description`` derived; ``tags``;
    ``timestamp`` from ``updated``) plus every Mnesis field as a tolerated extension key.
    ``id`` is retained as an alias of the path-derived concept identity."""
    meta: dict = {
        "type": page.kind,                              # OKF core: concept type ← Mnesis kind
        "title": page.title,
        "description": _first_sentence(page.body) or page.title,
        "timestamp": page.updated,                      # OKF core: last meaningful change
        "tags": list(page.tags),
    }
    # Mnesis extensions (tolerated; preserved verbatim on round-trip).
    for f in MNESIS_EXTENSION_FIELDS:
        v = getattr(page, f, None)
        if f == "question" and v is None:
            continue  # digest-only; keep non-digest frontmatter clean
        meta[f] = v
    return meta


def to_okf_document(page) -> str:
    """Render a :class:`Page` as an OKF-conformant Markdown document: OKF-core frontmatter
    (+ Mnesis extensions) and a body carrying generated OKF cross-links. Pure; no disk I/O.
    Frontmatter key order is preserved (OKF core first) — not alphabetized."""
    post = frontmatter.Post(render_okf_body(page), **to_okf_metadata(page))
    return frontmatter.dumps(post, sort_keys=False)
