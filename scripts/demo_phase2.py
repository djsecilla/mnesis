#!/usr/bin/env python
"""Phase-2 lifecycle demo: confidence, reinforcement, supersession, contradiction
review, and decay — fully offline (stub LLM).

Runs in a throwaway temp wiki + git repo so it never touches the project's own
`wiki/` or history. Relation classification is driven deterministically by a
`relation:<label>` marker in each source (the offline stub keys on it).

Run it with:  uv run python scripts/demo_phase2.py
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone

# Configure BEFORE importing the package (config reads env at import time).
_TMP = tempfile.mkdtemp(prefix="mnesis-phase2-")
os.environ["WIKI_LLM_STUB"] = "1"
os.environ["WIKI_ROOT"] = os.path.join(_TMP, "wiki")
# Raise the stale threshold so an aged single-source page can fall stale in the
# demo (a 1-source page's confidence asymptotes to ~0.25 under the default).
os.environ["WIKI_STALE_THRESHOLD"] = "0.5"

from mnesis import config, confidence, ingest, mcp_server, search, state, store  # noqa: E402
from mnesis.store import Page  # noqa: E402

TITLE = "Project Atlas uses Redis for caching"


def _hr(title: str) -> None:
    print("\n" + "=" * 70 + "\n" + title + "\n" + "=" * 70)


def _conf(page_id: str) -> float:
    page = store.read_page(page_id)
    return confidence.compute_confidence(page, access=state.get_access(page_id))[0]


def main() -> None:
    config.ensure_dirs()
    subprocess.run(["git", "-C", _TMP, "init", "-q"], check=True)
    subprocess.run(["git", "-C", _TMP, "config", "user.name", "mnesis demo"], check=True)
    subprocess.run(["git", "-C", _TMP, "config", "user.email", "demo@localhost"], check=True)
    print(f"Demo wiki root: {config.WIKI_ROOT}  (offline stub mode)")

    _hr("STEP 1 — Ingest a claim -> page A (moderate confidence)")
    a = ingest.ingest_source(f"{TITLE}.", "atlas-arch")
    conf_a0 = _conf(a.id)
    print(f"page A: {a.id}\n  source_count={a.source_count}  confidence={conf_a0:.2f}")

    _hr("STEP 2 — Ingest an agreeing source -> A reinforced (still ONE page)")
    r = ingest.ingest_source(
        f"{TITLE}. A second team confirms this independently. relation:reinforces", "atlas-confirm"
    )
    print(f"reinforced page: {r.id}")
    print(f"  source_count={r.source_count}  confidence={_conf(r.id):.2f}  (was {conf_a0:.2f} → rises)")
    print(f"  total pages: {len(store.list_pages())}  (no new page created)")

    _hr("STEP 3 — Ingest an updating source -> page B supersedes A (A → stale)")
    b = ingest.ingest_source(
        f"{TITLE}. relation:supersedes Atlas now uses Memcached for caching.", "atlas-update"
    )
    print(f"page B: {b.id}  supersedes={b.supersedes}")
    print(f"page A ({a.id}) status: {store.read_page(a.id).status}")

    _hr('STEP 4 — Query "redis caching"')
    print("default (active only) — B ranks, A is hidden:")
    print(mcp_server.wiki_query("redis caching"))
    print("\nwith include_stale=True — A reappears, demoted:")
    print(mcp_server.wiki_query("redis caching", include_stale=True))

    _hr("STEP 5 — Low-margin conflicting source -> review queue -> resolve")
    c = ingest.ingest_source(
        f"{TITLE}. relation:contradicts Actually Atlas uses Postgres for caching.", "atlas-conflict"
    )
    print(f"conflicting page C: {c.id}  (coexists, cross-linked, queued)\n")
    print(mcp_server.wiki_review())
    review_id = state.list_open_reviews()[0]["id"]
    print(f"\nResolving review #{review_id}, keeping {b.id}:")
    print("  " + mcp_server.wiki_resolve(review_id, b.id))
    print(f"  page C status: {store.read_page(c.id).status}")
    print(f"  open reviews now: {len(state.list_open_reviews())}")

    _hr("STEP 6 — Decay: an aged, unread fixture page transitions to stale")
    aged = Page(
        id="legacy-runbook",
        title="Legacy deploy runbook for the old stack",
        body="An old runbook nobody has confirmed or read in a long time.",
        sources=["legacy-wiki"],
        source_count=1,
        last_confirmed=(datetime.now(timezone.utc) - timedelta(days=400)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        ),
    )
    store.write_page(aged)
    search.upsert(aged)
    print(f"before: {aged.id}  status={store.read_page(aged.id).status}  confidence={_conf(aged.id):.2f}")
    print("running `mnesis decay`:")
    print("  " + mcp_server.wiki_decay())
    print(f"after:  {aged.id}  status={store.read_page(aged.id).status}")

    _hr("Final state of the wiki")
    print(mcp_server.wiki_list())
    print(f"\nDone. (Throwaway demo data left at {_TMP})")


if __name__ == "__main__":
    main()
