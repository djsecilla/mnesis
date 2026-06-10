# CLAUDE.md — mnesis (MVP PoC)

**This is the schema document: the most important file in the system.** It is the operating contract that turns a general-purpose LLM into a disciplined knowledge worker for this repository. It is read by the coding agent (Claude Code) as project context, and its rules govern how the runtime ingestion LLM writes and curates knowledge.

**Prime rule: keep this file in sync with the code.** If a change to the code alters a field name, a convention, a directory, or a behaviour described here, update this file in the *same* commit. When this file and the code disagree, this file is the intended design and the code is the bug.

---

## 1. What this project is

mnesis is a knowledge base that **compounds** instead of resetting. Retrieval-augmented generation fetches context and forgets it; this wiki accumulates and reinforces it. This repository is the **Phase-1 MVP proof of concept**, implementing the full end-to-end loop:

> **filter → ingest → write a canonical page → index → query → file an answer back → query again and see it surface.**

That last step — a synthesized answer becoming durable, retrievable knowledge — is the compounding behaviour the PoC exists to demonstrate.

---

## 2. Prime directives (invariants)

These hold across every module and every change. Treat a violation as a defect.

1. **Markdown is canonical; the search index is a rebuildable cache.** The Markdown pages under `wiki/pages/` are the single source of truth. The SQLite index is a projection of them and must be fully reconstructable from Markdown alone. Never persist anything in the index that is not derivable from a page.
2. **Filter sensitive data before any write.** Secrets and PII are redacted at the ingestion boundary, *before* a source is persisted or sent to the LLM. A raw secret value must never reach disk, logs, an LLM prompt, or a findings report.
3. **Record derived-state inputs; do not compute decay yet.** Confidence inputs (`source_count`, `last_confirmed`) are stored now as a seam. Confidence scoring and time decay are Phase 2 — do not implement them in the PoC.
4. **The git history is the audit trail.** Every page mutation is a commit. Do not batch unrelated writes into one commit, and do not rewrite history.
5. **Keep this file in sync** (see Prime rule above).

---

## 3. Repository conventions

Layout (src-layout; everything importable as `mnesis.*`):

```
README.md
CLAUDE.md                 # this file
pyproject.toml            # deps + the `mnesis` console script
.gitignore
.mcp.json                 # MCP registration for Claude Code
src/mnesis/
  config.py               # paths + env config (model, threshold, stub flag)
  store.py                # canonical Markdown + frontmatter + git
  filters.py              # secret / PII redaction (pure functions)
  llm.py                  # Anthropic client wrapper, with offline stub
  ingest.py               # pipeline: filter -> persist source -> extract -> write
  search.py               # SQLite FTS5 index: rebuild / upsert / search
  mcp_server.py           # FastMCP server exposing the wiki tools
  cli.py                  # `mnesis` command
wiki/
  pages/                  # canonical Markdown pages (tracked)
  sources/                # redacted raw sources, for provenance (tracked)
  .index/                 # SQLite index — GITIGNORED (rebuildable cache)
tests/
scripts/demo_end_to_end.py
```

**Environment variables** (read in `config.py`, all with fallbacks):

| Variable | Default | Purpose |
|---|---|---|
| `WIKI_ROOT` | `./wiki` | Root of pages, sources, and index. |
| `WIKI_LLM_MODEL` | `claude-sonnet-4-6` | Model used by the ingestion/extraction LLM. |
| `WIKI_FILEBACK_THRESHOLD` | `0.7` | Quality gate for filing answers back. |
| `WIKI_LLM_STUB` | unset | When `1` (or no API key), the LLM client returns deterministic canned output so tests and the demo run offline. |

`wiki/.index/` is never tracked by git — it is a cache that `mnesis rebuild` regenerates from the pages.

---

## 4. The page frontmatter schema

Every page is a Markdown file at `wiki/pages/<id>.md` with YAML frontmatter followed by a human-readable Markdown body. **These field names are authoritative** — the `store.Page` model, `ingest.py`, and `search.py` must match them exactly.

| Field | Type | Required | Default | Meaning |
|---|---|---|---|---|
| `id` | string (slug) | yes | — | Stable identifier; collision-safe slug of the title. |
| `title` | string | yes | — | One-line, declarative statement of what the page asserts. |
| `created` | ISO 8601 | yes | now | First write time; never changes. |
| `updated` | ISO 8601 | yes | now | Refreshed on every write. |
| `sources` | list[string] | yes | — | Source references (ids of files in `wiki/sources/`, or URLs). |
| `source_count` | int | yes | `1` | Number of distinct sources supporting the page. *(Confidence input — recorded, not yet scored.)* |
| `last_confirmed` | ISO 8601 | yes | now | When the claim was last confirmed by a source. *(Confidence input.)* |
| `tags` | list[string] | yes | `[]` | `type:value` tags (see §6) plus free tags. |
| `kind` | enum | yes | `fact` | `fact` \| `digest` \| `note` (see §5). |
| `status` | enum | yes | `active` | `active` \| `stale`. Stale pages are deprioritised, not deleted. |
| `supersedes` | string \| null | no | `null` | Id of the page this one replaces. |
| `superseded_by` | string \| null | no | `null` | Id of the page that replaced this one. |
| `question` | string | digest only | — | For `digest` pages: the question that produced the filed answer. |

The **body** is clean Markdown prose. It carries no index or search metadata — those live only in the (rebuildable) index.

Timestamps (`created`, `updated`, `last_confirmed`) are written in UTC ISO 8601 with **microsecond precision** and a `Z` suffix (e.g. `2026-06-10T17:25:20.118087Z`); the appendix examples elide the fraction for readability. `question` is emitted **only** for `digest` pages — `fact`/`note` frontmatter omits it. This is exactly the implemented `store.Page` model.

---

## 5. Page kinds

- **`fact`** — A discrete, sourced claim ingested from external material. The default output of the ingestion pipeline.
- **`digest`** — A synthesized answer filed back via `wiki_file_back` (crystallisation-lite): a question, the answer, and the facts it drew on. This is how exploration becomes durable knowledge. Always tagged so it is distinguishable from ingested facts.
- **`note`** — A human- or agent-authored observation that is neither a single sourced fact nor a synthesized answer. Used sparingly.

---

## 6. Domain vocabulary (entity & relationship types)

The typed knowledge graph is **deferred to Phase 3**, but tag consistently *now* so the graph can be built later with zero re-tagging. Encode entities as `type:value` tags on every page.

**Entity types:** `person`, `project`, `library`, `concept`, `file`, `decision`.
Example tags: `project:atlas`, `library:redis`, `person:sarah`, `concept:caching`, `decision:auth-migration`.

**Relationship types** (for Phase 3 edges; for now, express them in the page body, not as structured edges): `uses`, `depends_on`, `contradicts`, `caused`, `fixed`, `supersedes`, `owns`.

Use lowercase, hyphenated values. Prefer an existing tag over inventing a near-duplicate (`library:redis`, never also `lib:Redis`).

---

## 7. Ingest rules

The pipeline contract (`ingest.py`), in order:

1. **Scrub first.** Run `filters.scrub` on the raw text. Proceed with the redacted text only.
2. **Persist the source.** Save the redacted source via `store.write_source`, which writes `wiki/sources/<source_ref>.md` and commits it as `mnesis: source <source_ref>` for provenance.
3. **Extract.** Call the LLM with the disciplined extraction prompt to produce JSON `{title, summary_markdown, key_facts, tags}`. Parse robustly: strip code fences; on failure, retry once with a stricter instruction; then fall back to a minimal page built directly from the source.
4. **Write.** Build a `fact` page (`source_count: 1`, `last_confirmed: now`, `sources: [source_ref]`) and write it through `store.write_page`, which commits it as `mnesis: write <id>`. (Indexing into the search cache via `search.upsert` is done by the calling interface — CLI/MCP — not by the store.)

**Extraction discipline** (these go in the extraction system prompt):
- Cite the source. State only what the source supports.
- Do not invent facts, names, numbers, or relationships. Mark uncertainty explicitly in the body.
- Write a declarative `title` that states the claim (e.g. "Project Atlas uses Redis for caching"), not a topic label.
- Prefer one coherent claim per page. Group tightly related facts; split unrelated ones.

**Create-new vs. update-existing (PoC simplification):** In this PoC, **every ingested source creates a new `fact` page.** Detecting that a new source *reinforces* an existing page (incrementing `source_count`, refreshing `last_confirmed`), *contradicts* it, or should *supersede* it is **Phase 2** — leave the seam (`supersede()` exists in `store.py`) but do not wire automatic reinforcement now.

---

## 8. Retrieval contract

- Search is **BM25 keyword only** over `(id, title, tags, body)` via SQLite FTS5. Vector similarity, graph traversal, and reciprocal rank fusion are **out of scope** for the PoC.
- `search(query, limit)` returns ranked hits with `id`, `title`, `score`, and a `snippet`.
- The index is rebuilt from Markdown by `rebuild()`. `mnesis rebuild` after deleting `wiki/.index/` must reproduce identical results — this is the canonical-vs-cache invariant, and there is a test that asserts it.

---

## 9. File-back / crystallisation rule

`wiki_file_back(question, answer, quality_score=None)` is the compounding mechanism:

- If `quality_score` (or a simple internal heuristic when `None`) **≥ `WIKI_FILEBACK_THRESHOLD`**, write a `digest` page that records the `question`, the answer (body), and the facts it drew on (`sources`/`tags`). Return its id.
- Otherwise, **do not file**; return the reason ("below threshold").
- Digest pages are tagged `kind:digest` (and may carry `concept:` tags) so they never masquerade as primary sourced facts.

---

## 10. Quality standards for pages

A page is acceptable when it: states a clear, declarative claim in the `title`; is supported by at least one entry in `sources`; carries consistent `type:value` tags; contains no redaction leak; and does not duplicate an existing page's claim verbatim. Pages that fail are flagged rather than silently kept. (Automated LLM-as-judge scoring at scale is Phase 5; the PoC's bar is the checks above.)

---

## 11. Contradiction handling

PoC behaviour is **flag, don't resolve**: if ingestion notices a direct conflict with an existing page, note it in the body and tag both pages for review. Automatic resolution — proposing which claim wins by source recency, authority, and support count, with high-confidence cases applied automatically and low-confidence cases routed to a human — is **Phase 2/5**.

---

## 12. Privacy & governance

- **Filter on ingest** is mandatory and automatic (§2.2). The redaction must never leak the original value, including in the findings report.
- **Audit** is the git history plus the saved (redacted) sources. Every write is one commit.
- **Reversibility:** prefer `status: stale` over deletion. The PoC does not hard-delete pages.
- The MVP filter (regex + entropy) is intentionally simple; `detect-secrets` and Microsoft Presidio are the production upgrade path.

---

## 13. Scope: in vs. deferred

**In scope (this PoC — implemented):** filtered ingest · Markdown + git canonical store · FTS5 keyword search (rebuildable) · MCP interface with `wiki_ingest` / `wiki_query` / `wiki_file_back` (+ `wiki_get`, `wiki_list`, `wiki_rebuild`) · `mnesis` CLI · end-to-end demo and test. All present and exercised by the test suite.

**Out of scope for this PoC — map of where each deferred capability lands:**

| Capability | Phase |
|---|---|
| Confidence scoring & Ebbinghaus decay; supersession lifecycle | 2 |
| Entity extraction & typed-relationship knowledge graph | 3 |
| Automation hooks (on-source/session/query/schedule) & scheduler | 4 |
| Vector stream + reciprocal rank fusion; LLM-as-judge quality scoring | 5 |
| Multi-agent mesh sync; private/shared scoping | 6 |

When you extend the PoC toward any of these, **update this file first**, then make the code follow it.

---

## 14. Changing this file

This document is co-evolved with the system. Expect the first version to be rough and to sharpen after the first few dozen sources and lint passes. Conventions:

- Any code change that touches a field, directory, env var, tool, or behaviour described here updates this file in the same commit.
- Add new conventions under the section they belong to; don't scatter them.
- Keep it scannable — it is read at the start of every agent session.

---

## Appendix — Example `fact` page

```markdown
---
id: project-atlas-redis-cache
title: Project Atlas uses Redis for caching
created: 2026-06-09T10:15:00Z
updated: 2026-06-09T10:15:00Z
sources:
  - atlas-architecture-notes
source_count: 1
last_confirmed: 2026-06-09T10:15:00Z
tags: [project:atlas, library:redis, concept:caching, person:sarah]
kind: fact
status: active
supersedes: null
superseded_by: null
---
Project Atlas uses Redis as its primary caching layer. The auth-migration work
stream depends on this cache. Sarah owns the auth migration.

Source: atlas-architecture-notes. No contradicting sources at time of writing.
```

## Appendix — Example `digest` page (filed back)

```markdown
---
id: what-depends-on-redis-in-atlas
title: What depends on Redis in Project Atlas
created: 2026-06-09T11:02:00Z
updated: 2026-06-09T11:02:00Z
sources:
  - project-atlas-redis-cache
  - atlas-auth-migration-notes
source_count: 2
last_confirmed: 2026-06-09T11:02:00Z
tags: [project:atlas, library:redis, concept:caching, kind:digest]
kind: digest
status: active
supersedes: null
superseded_by: null
question: What depends on Redis in Project Atlas?
---
The Redis cache underpins Atlas's caching layer, and the auth-migration work
stream depends on it. Upgrading or replacing Redis therefore puts the auth
migration at risk and should be coordinated with Sarah, who owns it.

Synthesized from: project-atlas-redis-cache, atlas-auth-migration-notes.
```
