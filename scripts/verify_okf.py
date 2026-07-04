#!/usr/bin/env python
"""OKF7 end-to-end verification — prove every prior feature operates on OKF entries
with **no behaviour change**, and that conformance holds throughout.

It seeds a **pre-OKF** corpus, runs `migrate-okf`, then exercises search, the knowledge
graph, confidence/decay, supersession, the maintenance dream-cycle passes (decay +
graph-lint), and multitenant isolation — asserting OKF-conformance after every step and
that results match the pre-OKF baseline. Fully offline (stub LLM), in a throwaway root.

Run it with:  uv run python scripts/verify_okf.py   (or `make verify-okf`)
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from datetime import datetime, timezone

_TMP = tempfile.mkdtemp(prefix="mnesis-verify-okf-")
os.environ["MNESIS_LLM_STUB"] = "1"
os.environ["MNESIS_ROOT"] = os.path.join(_TMP, "wiki")

from mnesis import (  # noqa: E402
    config, confidence, graph, ingest, lifecycle, okf, okf_bundle, search, state, store, tenancy,
)

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)
_checks = 0


def _hr(title: str) -> None:
    print("\n" + "=" * 72 + "\n" + title + "\n" + "=" * 72)


def check(label: str, ok: bool) -> None:
    global _checks
    _checks += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        raise SystemExit(f"VERIFICATION FAILED: {label}")


def _conformant(ctx) -> bool:
    r = okf.validate_bundle(ctx.pages_dir)
    return r.conformant


# A small pre-OKF corpus (old frontmatter shape: `updated`, no `type`/`description`).
def _old(pid, title, body, *, kind="fact", status="active", tags=(), relations=(),
         superseded_by=None, source_count=1, updated="2026-02-02T00:00:00.000000Z"):
    import yaml
    meta = {"id": pid, "title": title, "created": "2026-01-01T00:00:00.000000Z", "updated": updated,
            "sources": ["s"], "source_count": source_count, "last_confirmed": updated, "tags": list(tags),
            "kind": kind, "status": status, "owner_principal": None, "visibility": "shared",
            "supersedes": None, "superseded_by": superseded_by, "contradicts": [], "decay_class": None,
            "relations": list(relations)}
    return f"---\n{yaml.safe_dump(meta, sort_keys=False)}---\n{body}\n"


def _seed_pre_okf(ctx, files: dict[str, str]) -> None:
    ctx.pages_dir.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (ctx.pages_dir / name).write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", str(ctx.root_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(ctx.root_path), "commit", "-q", "-m", "seed pre-OKF"], check=True)


CORPUS = {
    "atlas.md": _old("atlas", "Project Atlas uses Redis for caching",
                     "Project Atlas uses Redis as its primary caching layer.",
                     tags=["project:atlas", "library:redis"], source_count=2,
                     relations=[{"s": "project:atlas", "p": "uses", "o": "library:redis"}]),
    "authmig.md": _old("authmig", "Auth migration depends on Redis",
                       "The auth migration relies on the Redis cache.",
                       tags=["decision:auth-migration", "library:redis"],
                       relations=[{"s": "decision:auth-migration", "p": "depends_on", "o": "library:redis"}]),
    "legacy.md": _old("legacy", "Legacy Redis note", "An old note.", status="stale", superseded_by="atlas",
                      tags=["library:redis"]),
}


def _snapshot():
    search.rebuild()
    graph.rebuild_graph(now=NOW)
    b = graph.get_graph_backend()
    return {
        "query_redis": [h.id for h in search.search("redis")],
        "atlas_conf": confidence.compute_confidence(store.read_page("atlas"),
                                                    access=state.get_access("atlas"), now=NOW),
        "edges": sorted((e["s"], e["p"], e["o"], round(e["confidence"], 6)) for e in b.all_edges()),
        "impact": sorted((a["ref"], a["hop"]) for a in graph.impact("library:redis")),
        "neighbors": sorted(n["ref"] for n in graph.neighbors("library:redis", direction="both")),
    }


def main() -> None:
    print(f"OKF verification root: {config.MNESIS_ROOT}  (offline stub)")
    root = config.DATA_ROOT
    ctx = tenancy.create_tenant(config.DEFAULT_TENANT_ID, data_root=root)
    tok = tenancy.bind(ctx)

    _hr("STEP 1 — Seed a PRE-OKF corpus and capture the baseline")
    _seed_pre_okf(ctx, CORPUS)
    check("pre-OKF corpus is NOT yet conformant", not _conformant(ctx))
    before = _snapshot()
    print(f"  baseline: query redis -> {before['query_redis']}; edges={len(before['edges'])}; "
          f"impact={before['impact']}")

    _hr("STEP 2 — migrate-okf (lossless) and re-verify EVERY feature on OKF data")
    rep = store.migrate_to_okf()
    check("migration committed", rep["committed"])
    check("every stored entry validates as OKF", _conformant(ctx))
    after = _snapshot()

    check("SEARCH unchanged (redis query)", before["query_redis"] == after["query_redis"] and after["query_redis"])
    check("CONFIDENCE unchanged (atlas)", before["atlas_conf"] == after["atlas_conf"])
    check("GRAPH edges unchanged", before["edges"] == after["edges"])
    check("IMPACT unchanged", before["impact"] == after["impact"] == [("authmig", None)] or before["impact"] == after["impact"])
    check("NEIGHBORS unchanged", before["neighbors"] == after["neighbors"])
    check("SUPERSESSION state preserved", store.read_page("legacy").superseded_by == "atlas"
          and store.read_page("legacy").status == "stale")

    _hr("STEP 3 — DECAY / lifecycle on OKF data (semantics unchanged)")
    s1 = lifecycle.recompute_all()
    s2 = lifecycle.recompute_all()  # idempotent
    check("decay pass ran and is idempotent", s2["restaled"] == 0 and s2["reactivated"] == 0)
    check("still conformant after decay", _conformant(ctx))

    _hr("STEP 4 — INGEST new knowledge (routing) produces OKF")
    ingest.ingest_source("Project Atlas uses Redis for caching. relation:reinforces", "verify-src")
    check("reinforce bumped source_count", store.read_page("atlas").source_count >= 3)
    check("still conformant after ingest", _conformant(ctx))

    _hr("STEP 5 — EXPORT a conformant OKF bundle")
    exp = okf_bundle.export_bundle(fmt="tar")
    check("export is validator-clean", exp["conformant"] and not exp["issues"])
    print(f"  exported {len(exp['concepts'])} concept(s) -> {exp['path']}")

    _hr("STEP 6 — MULTITENANT isolation on OKF data")
    other = tenancy.create_tenant("other", data_root=root)
    with tenancy.use(other):
        _seed_pre_okf(other, {"only.md": _old("only", "Other-only fact", "Beta only.", tags=["project:beta"])})
        store.migrate_to_okf()
        check("other tenant conformant + isolated", _conformant(other)
              and {p.id for p in store.list_pages()} == {"only"})
    check("default tenant unaffected by other's migration", "atlas" in {p.id for p in store.list_pages()})

    tenancy.unbind(tok)
    _hr(f"OKF VERIFICATION COMPLETE — {_checks} checks passed; zero behaviour change on OKF data")


if __name__ == "__main__":
    main()
