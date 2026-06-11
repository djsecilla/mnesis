#!/usr/bin/env python
"""Phase-3 graph demo: entities, typed relations, impact traversal, supersession,
and graph lint — fully offline (stub LLM).

Runs in a throwaway temp wiki + git repo so it never touches the project's own
`wiki/` or history. Entity/relation extraction is driven deterministically by
`tag{...}` / `rel{s|p|o}` markers (and `relation:<label>` for lifecycle routing).

Run it with:  uv run python scripts/demo_phase3.py
"""

from __future__ import annotations

import os
import subprocess
import tempfile

_TMP = tempfile.mkdtemp(prefix="mnesis-phase3-")
os.environ["MNESIS_LLM_STUB"] = "1"
os.environ["MNESIS_ROOT"] = os.path.join(_TMP, "wiki")

from mnesis import config, graph, ingest, mcp_server  # noqa: E402


def _hr(title: str) -> None:
    print("\n" + "=" * 70 + "\n" + title + "\n" + "=" * 70)


def main() -> None:
    config.ensure_dirs()
    subprocess.run(["git", "-C", _TMP, "init", "-q"], check=True)
    subprocess.run(["git", "-C", _TMP, "config", "user.name", "mnesis demo"], check=True)
    subprocess.run(["git", "-C", _TMP, "config", "user.email", "demo@localhost"], check=True)
    print(f"Demo wiki root: {config.MNESIS_ROOT}  (offline stub mode)")

    _hr("STEP 1 — Ingest sources establishing entities and relations")
    # A dependency chain: Atlas -> auth-migration -> Redis, plus Sarah owns it.
    ingest.ingest_source(
        "Project Atlas depends on the authentication migration. "
        "tag{project:atlas} tag{decision:auth-migration} "
        "rel{project:atlas|depends_on|decision:auth-migration}",
        "atlas-overview",
    )
    ingest.ingest_source(
        "The authentication migration depends on the Redis cache. "
        "tag{decision:auth-migration} tag{library:redis} "
        "rel{decision:auth-migration|depends_on|library:redis}",
        "auth-migration-notes",
    )
    ingest.ingest_source(
        "Sarah owns the authentication migration. "
        "tag{person:sarah} tag{decision:auth-migration} "
        "rel{person:sarah|owns|decision:auth-migration}",
        "ownership",
    )
    print("ingested 3 sources.")
    print("note: the Atlas page mentions only the auth migration — it never says 'Redis'.")

    _hr("STEP 2 — `mnesis rebuild` (search index AND graph)")
    print(mcp_server.mnesis_rebuild())
    print(f"active graph backend: {config.GRAPH_BACKEND}")
    print(mcp_server.mnesis_graph_stats())

    _hr("STEP 3 — Impact of upgrading Redis (graph traversal)")
    print(mcp_server.mnesis_impact("library:redis"))
    print("\n-> Atlas surfaces transitively, through auth-migration — a Redis dependency")
    print("   its own page never states in words.")

    _hr("STEP 4 — Ingest an update: the migration moves from Redis to Postgres")
    ingest.ingest_source(
        "The authentication migration depends on the Redis cache. relation:supersedes "
        "The migration now uses Postgres instead. "
        "tag{decision:auth-migration} tag{library:postgres} "
        "rel{decision:auth-migration|depends_on|library:postgres}",
        "auth-migration-postgres",
    )
    print(mcp_server.mnesis_rebuild())
    print("\nimpact of upgrading Redis now (old edge demoted, superseded page stale):")
    print(mcp_server.mnesis_impact("library:redis"))
    print("\nimpact of upgrading Postgres now (the superseding page's edge took over):")
    print(mcp_server.mnesis_impact("library:postgres"))

    _hr("STEP 5 — `mnesis graph-lint --fix`")
    print(mcp_server.mnesis_graph_lint(fix=True))

    _hr("Final graph")
    print(mcp_server.mnesis_graph_stats())
    print(f"\nDone. (Throwaway demo data left at {_TMP})")


if __name__ == "__main__":
    main()
