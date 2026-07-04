"""OKF bundle export / import (OKF6) — interop with the wider OKF ecosystem.

A tenant's ``pages/`` directory already *is* a conformant OKF bundle (OKF2/OKF3), so:

  - **export** ensures it is OKF (idempotent migration) and emits it as a self-contained
    directory or ``.tar.gz`` — concept docs + the reserved ``index.md``/``log.md`` — that
    passes the validator (and Google's reference-parser field expectations).
  - **import** brings an *external* OKF bundle in **through the normal governed ingest
    path** (redaction → extract → route → review). Imported bundle content is **UNTRUSTED
    data, never instructions**: each concept's text is fed to the ingest pipeline exactly
    like any other source (the writing-agent safety posture), and the bundle's frontmatter
    is **not** trusted or written directly — Mnesis re-derives everything and redacts it.

Both operate on the **active tenant** (per-tenant by construction).
"""

from __future__ import annotations

import shutil
import tarfile
import tempfile
from pathlib import Path

import frontmatter

from . import ingest, okf, store, tenancy

#: A concept doc without a body carries no knowledge to ingest — skipped on import.
_MIN_BODY_CHARS = 1


def _concept_docs(root: Path) -> list[Path]:
    """Every concept ``.md`` under ``root`` (the reserved index.md/log.md are excluded)."""
    return sorted(p for p in root.rglob("*.md") if p.name not in store.RESERVED_PAGE_FILES)


# --- export ----------------------------------------------------------------


def export_bundle(dest: str | Path | None = None, *, fmt: str = "dir") -> dict:
    """Export the active tenant's knowledge as a conformant OKF bundle.

    ``fmt`` is ``"dir"`` (a directory copy) or ``"tar"`` (a ``.tar.gz``). Ensures the
    bundle is OKF-conformant first (idempotent), validates it, and returns a summary
    ``{path, format, concepts, conformant, issues}``."""
    ctx = tenancy.current()
    s = store.Store(ctx)
    s.migrate_to_okf()  # idempotent: guarantees OKF docs + current index.md/log.md
    src = ctx.pages_dir
    report = okf.validate_bundle(src)
    files = sorted(src.glob("*.md")) if src.exists() else []
    concepts = [p.stem for p in files if p.name not in store.RESERVED_PAGE_FILES]

    if fmt == "tar":
        out = Path(dest) if dest else ctx.cache_dir / f"{ctx.tenant_id}-okf-bundle.tar.gz"
        out.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(out, "w:gz") as tar:
            for p in files:
                tar.add(p, arcname=p.name)  # flat bundle: concept.md at the root
    else:
        out = Path(dest) if dest else ctx.cache_dir / "okf-export"
        out.mkdir(parents=True, exist_ok=True)
        for p in files:
            shutil.copy2(p, out / p.name)

    return {
        "path": str(out),
        "format": fmt,
        "concepts": concepts,
        "conformant": report.conformant,
        "issues": [str(i) for i in report.errors],
    }


# --- import (governed, untrusted) ------------------------------------------


def _extract_bundle(src: Path) -> tuple[Path, Path | None]:
    """Resolve ``src`` to a bundle root directory, extracting a tarball **safely**
    (Python's ``data`` filter blocks path traversal / absolute members). Returns
    ``(root, tempdir_to_cleanup)``."""
    if src.is_dir():
        return src, None
    if tarfile.is_tarfile(src):
        tmp = Path(tempfile.mkdtemp(prefix="okf-import-"))
        with tarfile.open(src) as tar:
            tar.extractall(tmp, filter="data")  # untrusted archive → safe extraction
        # If the archive nested everything under a single dir, descend into it.
        entries = [p for p in tmp.iterdir()]
        root = entries[0] if len(entries) == 1 and entries[0].is_dir() else tmp
        return root, tmp
    raise ValueError(f"not an OKF bundle (directory or .tar.gz expected): {src}")


def _safe_source_ref(concept_id: str) -> str:
    """A filesystem/git-safe source ref for an imported concept (no path separators)."""
    return store.slugify(concept_id.replace("/", "-")) or "okf-import"


def import_bundle(src: str | Path, *, prefix: str = "okf") -> dict:
    """Import an external OKF bundle into the active tenant **through the governed ingest
    pipeline** (redaction, extraction, routing, review). Each concept's text is treated as
    **untrusted source data** — its frontmatter is never trusted; Mnesis re-derives and
    redacts everything. Returns a summary with per-concept routing + total redactions."""
    src = Path(src)
    root, tmp = _extract_bundle(src)
    try:
        docs = _concept_docs(root)
        results: list[dict] = []
        redactions = 0
        for p in docs:
            cid = okf.concept_id(p, root)
            try:
                post = frontmatter.load(str(p))
            except Exception:  # noqa: BLE001 — a malformed doc is skipped, not fatal
                results.append({"concept": cid, "status": "skipped", "reason": "unparseable"})
                continue
            body = (post.content or "").strip()
            if len(body) < _MIN_BODY_CHARS:
                results.append({"concept": cid, "status": "skipped", "reason": "empty"})
                continue
            # UNTRUSTED DATA: build a plain source text from the concept's title + body and
            # push it through the SAME governed path as any ingest — nothing is executed,
            # no external frontmatter is written directly.
            title = str(post.metadata.get("title") or cid)
            text = f"{title}\n\n{body}".strip()
            ref = f"{prefix}-{_safe_source_ref(cid)}"
            plan = ingest.plan_ingest(text, ref)        # scrub happens here (redaction)
            outcome = ingest.apply_ingest(plan)         # extract -> route -> review -> write (OKF)
            redactions += int(outcome.get("redaction_count", 0))
            results.append({
                "concept": cid, "status": "imported", "page_id": outcome["page_id"],
                "action": outcome["action_taken"], "redactions": outcome["redaction_count"],
            })
        imported = [r for r in results if r.get("status") == "imported"]
        return {
            "source": str(src),
            "concepts": len(docs),
            "imported": len(imported),
            "redactions": redactions,
            "results": results,
        }
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
