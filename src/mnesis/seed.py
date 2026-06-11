"""Idempotent seeding of bundled sample sources, so a fresh deployment is
immediately queryable. Runs offline (stub LLM) by default.

Re-running is a no-op: a source whose provenance file already exists is skipped,
so no duplicate pages are created. Run in the container with
``python -m mnesis.seed`` (the ``docker-seed`` make target) or locally via
``scripts/seed.py``.
"""

from __future__ import annotations

import os

# Seed offline by default (no network); respect an explicit override.
os.environ.setdefault("MNESIS_LLM_STUB", "1")

from . import config, graph, ingest, search, store  # noqa: E402

# A small connected corpus: Atlas -> auth-migration -> Redis, Sarah owns the
# migration, and a separate billing/Postgres fact. Markers drive the offline
# stub's entity/relation extraction.
SAMPLE_SOURCES: list[tuple[str, str]] = [
    (
        "atlas-architecture",
        "Project Atlas depends on the authentication migration. "
        "tag{project:atlas} tag{decision:auth-migration} "
        "rel{project:atlas|depends_on|decision:auth-migration}",
    ),
    (
        "auth-migration-notes",
        "The authentication migration depends on the Redis cache. "
        "tag{decision:auth-migration} tag{library:redis} "
        "rel{decision:auth-migration|depends_on|library:redis}",
    ),
    (
        "ownership",
        "Sarah owns the authentication migration. "
        "tag{person:sarah} tag{decision:auth-migration} "
        "rel{person:sarah|owns|decision:auth-migration}",
    ),
    (
        "billing-notes",
        "The billing service stores invoices in PostgreSQL. "
        "tag{project:billing} tag{library:postgresql} "
        "rel{project:billing|uses|library:postgresql}",
    ),
]


def main() -> None:
    config.ensure_dirs()
    created, skipped = 0, 0
    for source_ref, text in SAMPLE_SOURCES:
        if (config.SOURCES_DIR / f"{source_ref}.md").exists():
            print(f"  skip {source_ref} (already seeded)")
            skipped += 1
            continue
        page = ingest.ingest_source(text, source_ref)
        print(f"  seeded {source_ref} -> {page.id}")
        created += 1

    pages = search.rebuild()
    g = graph.rebuild_graph()
    print(
        f"rebuilt caches: {pages} pages indexed; "
        f"graph {g['entities']} entities / {g['edges']} edges"
    )
    print(
        f"seed complete: {created} created, {skipped} skipped, "
        f"{len(store.list_pages())} pages total"
    )


if __name__ == "__main__":
    main()
