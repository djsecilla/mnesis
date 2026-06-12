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
    # A later source updating the billing fact: same claim subject, marked to
    # supersede. Ingest stales the original and links both ways, so the corpus
    # exercises the supersession lifecycle (active winner + stale predecessor).
    (
        "billing-notes-update",
        "The billing service stores invoices in PostgreSQL. relation:supersedes "
        "It now runs on a managed Aurora PostgreSQL cluster. "
        "tag{project:billing} tag{library:postgresql} "
        "rel{project:billing|uses|library:postgresql}",
    ),
]

# A synthesized answer to file back, so the corpus also contains a `digest`
# page (with its originating question) alongside ingested `fact` pages.
SAMPLE_DIGEST: tuple[str, str] = (
    "What does Project Atlas depend on?",
    "Project Atlas depends on the authentication migration, which in turn "
    "depends on the Redis cache. Sarah owns that migration, so changes to "
    "Redis or the auth work should be coordinated with her.",
)


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

    # File back a synthesized answer once, so the corpus has a digest page.
    # Guard on the digest kind already existing to keep re-seeding a no-op.
    if not store.list_pages(kind="digest"):
        from . import mcp_server  # lazy: pulls FastMCP only when seeding

        question, answer = SAMPLE_DIGEST
        result = mcp_server.mnesis_file_back(question, answer, 0.9)
        print(f"  filed digest -> {result}")
        created += 1
    else:
        print("  skip digest (already filed)")
        skipped += 1

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
