# LLM Wiki v2 — Claude Code Build Playbook

**A sequenced set of prompts for building the minimum-viable, end-to-end PoC with Claude Code (Opus 4.6).**

This playbook turns the LLM Wiki v2 reference architecture into a working proof of concept. The PoC implements the **Phase-1 MVP** from the roadmap — but wired into the *complete compounding loop* so you can watch knowledge accumulate:

> **filter → ingest → write canonical page → index → query → file an answer back → query again and see it surface.**

You run these prompts in order, one per Claude Code turn. Each builds on the last, ships tests, and commits. By the end you have a runnable system plus an MCP server your agents can call.

---

## What you'll have at the end

- A Python package (`llmwiki`) with a clean module per architectural concern.
- **Canonical layer:** Markdown pages with frontmatter, versioned in Git (the source of truth).
- **Ingest guard:** secret/PII redaction that runs *before* anything is written.
- **Ingestion pipeline:** raw source → LLM extraction → clean page (with an offline stub so tests need no network).
- **Retrieval:** an SQLite FTS5 keyword index that is a *rebuildable cache* of the Markdown.
- **Interface:** an MCP server exposing `wiki_ingest`, `wiki_query`, `wiki_file_back`, plus a CLI.
- **Proof:** an end-to-end demo and test asserting the loop compounds.

---

## Prerequisites

- Claude Code installed, with **Opus 4.6 selected** as the active model (e.g. via `/model`).
- Python 3.11+ and `git` on PATH.
- An empty working directory — start Claude Code there.
- For *real* (non-stub) ingestion: `ANTHROPIC_API_KEY` set. Every test and the demo run in **stub mode** and need no key and no network.

---

## How to use this playbook

1. Open Claude Code in your empty directory.
2. Paste **Prompt 0**. Let it scaffold, run tests, and commit. Skim the diff.
3. Continue with Prompts 1 → 7, one per turn. Review the diff and the test output after each.
4. If Claude Code proposes a reasonable deviation, let it — it's instructed to note assumptions and keep `CLAUDE.md` in sync.
5. Keep each turn focused on a single prompt; don't merge them. The sequence is designed so each step is independently testable.

**Every prompt follows the same template** (Daniel-standard, for consistency across the suite):

```
CONTEXT     — one line: what exists now and where this fits
OBJECTIVE   — the single goal of this step
BUILD       — files/modules to create or change, with responsibilities
CONSTRAINTS — conventions, scope boundaries, what NOT to do
ACCEPTANCE  — runnable checks that must pass
ON DONE     — run tests, commit (conventional message), report summary + assumptions
```

---

## Scope boundary

**In scope (this PoC):** filtered ingest · Markdown+Git canonical store · FTS5 keyword search (rebuildable) · MCP interface with ingest/query/file-back · CLI · end-to-end demo.

**Deliberately deferred (later phases, per the architecture roadmap):** confidence scoring & Ebbinghaus decay · supersession lifecycle · typed knowledge graph · vector + reciprocal-rank-fusion hybrid search · automation hooks & scheduler · LLM-as-judge quality scoring at scale · multi-agent mesh sync & private/shared scoping · multi-format output renderers.

The prompts leave clean seams for all of these (frontmatter fields, a rebuildable index, an MCP tool surface) without implementing them.

---

# The Prompts

---

## Prompt 0 — Bootstrap & schema

```
CONTEXT: New empty repository. We are building the MVP of "LLM Wiki v2," a compounding knowledge base for AI agents. This first task establishes the scaffold and the schema document that governs every later step.

OBJECTIVE: Create the project skeleton, dependency manifest, the CLAUDE.md schema document, and a stub package, with git initialized and a passing smoke test.

BUILD:
- Initialize a git repo with this src-layout tree:
    README.md
    CLAUDE.md
    pyproject.toml
    .gitignore
    src/llmwiki/__init__.py
    src/llmwiki/config.py
    src/llmwiki/cli.py            (placeholder main() for now)
    wiki/pages/.gitkeep
    wiki/sources/.gitkeep
    tests/test_smoke.py
    scripts/.gitkeep
- pyproject.toml: target Python 3.11+, dependencies anthropic, mcp, python-frontmatter, pyyaml, pytest. Console script: wiki = "llmwiki.cli:main".
- src/llmwiki/config.py: resolve repo-relative paths (WIKI_ROOT default ./wiki, PAGES_DIR, SOURCES_DIR, INDEX_DIR = wiki/.index). Read from env with fallbacks: WIKI_LLM_MODEL (default "claude-sonnet-4-6"), WIKI_FILEBACK_THRESHOLD (default 0.7), WIKI_LLM_STUB. Expose them as importable constants/functions.
- CLAUDE.md — THE SCHEMA DOCUMENT, the most important file in the system. Write it to cover: the domain entity/relationship vocabulary (people, projects, libraries, concepts, files, decisions; relationships uses/depends-on/contradicts/caused/fixed/supersedes) even though the graph is deferred; the page frontmatter schema (id, title, created, updated, sources, source_count, last_confirmed, tags, kind [fact|digest|note], status [active|stale], supersedes, superseded_by); ingest rules; the CANONICAL-VS-CACHE principle (Markdown is source of truth, the index is a rebuildable cache); what is in scope for this PoC and what is deferred to later phases; and conventions (pages in wiki/pages, raw sources in wiki/sources, index in wiki/.index which is gitignored). End with an instruction that every future task must keep CLAUDE.md in sync with the code.
- .gitignore: wiki/.index/, .venv, __pycache__, *.pyc, .env
- README.md: one-paragraph overview plus setup steps.
- tests/test_smoke.py: import llmwiki, assert config paths resolve and the wiki dirs are created on demand.

CONSTRAINTS:
- wiki/.index must NOT be tracked by git (it is a rebuildable cache).
- No business logic beyond config; later prompts fill the modules.
- Everything importable as llmwiki.*

ACCEPTANCE:
- `pip install -e .` succeeds. `pytest -q` passes. `git log` shows one commit. The tree matches the spec.

ON DONE: run tests, commit ("chore: bootstrap llm-wiki PoC scaffold and schema"), report the created tree and any assumptions.
```

---

## Prompt 1 — Canonical layer: Markdown store + Git

```
CONTEXT: The scaffold and CLAUDE.md exist. Build the canonical store: Markdown pages with YAML frontmatter, versioned in Git. This is the single source of truth.

OBJECTIVE: Implement src/llmwiki/store.py with create/read/update/list/supersede operations that persist pages as frontmatter-Markdown and commit each mutation to Git.

BUILD:
- A Page model (dataclass) with the exact frontmatter fields defined in CLAUDE.md: id, title, body, created, updated, sources (list), source_count (int), last_confirmed (iso), tags (list), kind, status, supersedes, superseded_by.
- write_page(page) -> path: serialize to wiki/pages/<id>.md via python-frontmatter; set/refresh updated; then git add + commit "wiki: write <id>".
- read_page(id) -> Page; list_pages(status=None, kind=None) -> [Page]; page_exists(id).
- supersede(old_id, new_page): write the new page with supersedes=old_id; flip the old page to status=stale and superseded_by=new_id; commit once. (Minimal — this is a seam for the Phase-2 lifecycle.)
- slugify(title) for ids, collision-safe (append -2, -3, ...).
- Use git via subprocess; if user.name/user.email are unset in the repo, configure a local PoC identity so commits never fail.

CONSTRAINTS:
- Frontmatter field names MUST match CLAUDE.md. If you must change them, update CLAUDE.md in the same commit.
- Never write outside wiki/pages. Keep page bodies clean human-readable Markdown — no search/index concerns here.

ACCEPTANCE:
- tests/test_store.py: create a page and read it back identical; list returns it; updating it bumps `updated` and creates a second commit; supersede flips status and links both directions. `pytest -q` passes; the repo log shows one commit per write.

ON DONE: run tests, commit ("feat: canonical markdown+git store"), report.
```

---

## Prompt 2 — Ingest guard: sensitive-data filter

```
CONTEXT: The store works. Per the governance principle, nothing reaches the store carrying secrets or PII. Build the filter that runs at the ingestion boundary.

OBJECTIVE: Implement src/llmwiki/filters.py that detects and redacts secrets and PII from text before any write, returning redacted text plus a findings report.

BUILD:
- scrub(text, allowlist=None) -> (redacted_text, findings):
    * Secrets: a high-entropy token heuristic PLUS regexes for common shapes — sk-... style keys, AWS AKIA..., bearer tokens, PEM "BEGIN ... PRIVATE KEY" blocks, long hex/base64 blobs. Replace each with [REDACTED:SECRET].
    * PII: regexes for emails, phone numbers, and credit-card-like digit sequences. Replace with [REDACTED:PII:<type>].
    * findings: a list of {type, kind, start, end} — and CRUCIALLY never the matched value itself.
- An optional allowlist to suppress known false positives.
- Pure functions, no I/O. A module docstring stating the conservative-by-default tradeoff and that detect-secrets / Microsoft Presidio are the production upgrade path.

CONSTRAINTS:
- The raw secret value must never appear in the return value, logs, or findings.
- Prefer over-redaction over leakage for a PoC.

ACCEPTANCE:
- tests/test_filters.py with fixtures containing a fake API key, an email, and a phone number: assert all are redacted; findings report the types without leaking values; clean text passes through unchanged. `pytest -q` passes.

ON DONE: run tests, commit ("feat: sensitive-data filter for ingest"), report.
```

---

## Prompt 3 — Ingestion pipeline + LLM client

```
CONTEXT: Store and filter exist. Build the pipeline that turns a raw source into a clean page: filter -> LLM extraction -> write. It must be testable offline.

OBJECTIVE: Implement src/llmwiki/llm.py (an Anthropic client wrapper with an offline stub) and src/llmwiki/ingest.py (the pipeline).

BUILD:
- llm.py: a thin wrapper over the `anthropic` SDK. complete(system, user) -> str using WIKI_LLM_MODEL. It MUST support a stub mode (when WIKI_LLM_STUB=1 or no API key is present) that returns a deterministic canned JSON structure, so tests and the demo never touch the network. Centralize model name and max_tokens here. Before coding, check the installed anthropic SDK's messages API shape and match it.
- ingest.py: ingest_source(raw_text, source_ref) -> Page:
    1. scrub(raw_text) via filters; proceed with the redacted text.
    2. Persist the redacted source to wiki/sources/<source_ref>.md for provenance.
    3. Call the LLM with a strict extraction prompt that returns JSON: {title, summary_markdown, key_facts: [...], tags: [...]}. Parse robustly: strip code fences; on parse failure retry once with a stricter instruction; then fall back to a minimal page built from the source.
    4. Build a Page (kind=fact, sources=[source_ref], source_count=1, last_confirmed=now) and write it via store.write_page.
- Keep the extraction prompt in a named constant and make it disciplined: cite the source, do not invent facts, mark uncertainty.

CONSTRAINTS:
- Filtering happens BEFORE the source is persisted or sent to the LLM.
- The full test suite must pass with WIKI_LLM_STUB=1 (no key, no network).

ACCEPTANCE:
- tests/test_ingest.py (stub mode): ingest a source containing a fake secret -> a page is created; the secret is absent from BOTH the page and the saved source; frontmatter is well-formed; a git commit exists. `pytest -q` passes.

ON DONE: run tests, commit ("feat: ingestion pipeline with stubbable LLM client"), report.
```

---

## Prompt 4 — Retrieval: SQLite FTS5 index (rebuildable cache)

```
CONTEXT: Pages are being written. Build keyword search over them as a rebuildable cache, strictly honoring the canonical-vs-cache principle.

OBJECTIVE: Implement src/llmwiki/search.py: an SQLite FTS5 index built FROM the Markdown pages, with query, incremental upsert, and full rebuild.

BUILD:
- DB at wiki/.index/wiki.db (gitignored). An FTS5 virtual table over (id, title, tags, body) using the unicode61/porter tokenizer.
- rebuild() -> int: drop and repopulate the index by reading every page via store.list_pages(); return the count. This is the source-of-truth -> cache projection.
- upsert(page): incremental index update for a single page (to be called after writes).
- search(query, limit=10) -> [SearchHit]: BM25-ranked via FTS5 bm25(); each hit has id, title, score, and a snippet via FTS5 snippet().
- If FTS5 is not compiled into the runtime's sqlite3, raise a clear error explaining the remediation.

CONSTRAINTS:
- The index must be fully reconstructable from Markdown alone. Never store anything in the DB that is not derivable from the pages.
- Keep ranking to plain bm25. Vectors, embeddings, and graph traversal are explicitly OUT OF SCOPE for this PoC.

ACCEPTANCE:
- tests/test_search.py: write 3 pages, rebuild, assert search returns the expected top hit with a non-empty snippet; then delete the DB file, rebuild again, and assert IDENTICAL results (proving the cache is rebuildable). `pytest -q` passes.

ON DONE: run tests, commit ("feat: FTS5 keyword search as rebuildable index"), report.
```

---

## Prompt 5 — Interface: MCP server

```
CONTEXT: All core modules exist. Expose them through the Model Context Protocol so Claude Code and other agents can use the wiki natively.

OBJECTIVE: Implement src/llmwiki/mcp_server.py with the official MCP Python SDK, exposing the wiki tools, plus a .mcp.json so this repo's Claude Code connects automatically.

BUILD:
- A FastMCP server (verify the import path against the installed `mcp` SDK — e.g. mcp.server.fastmcp.FastMCP — and match the version's decorator/registration API) exposing tools:
    * wiki_ingest(text, source_ref) -> created page summary (id, title, tags, redaction count).
    * wiki_query(query, limit=10) -> ranked hits (id, title, snippet, score).
    * wiki_get(id) -> the full page Markdown.
    * wiki_file_back(question, answer, quality_score=None) -> if quality_score (or a simple internal heuristic when None) >= WIKI_FILEBACK_THRESHOLD, write a kind=digest page that links the question and answer (crystallization-lite) and return its id; otherwise return "below threshold, not filed" with the reason.
    * wiki_list() and wiki_rebuild() as utilities.
- Tools return concise structured text. Server runs over stdio.
- .mcp.json registering the server (command = the project's python, args = ["-m", "llmwiki.mcp_server"]) so `claude` in this repo discovers it.
- Append an MCP section to README: how to run the server standalone, how Claude Code auto-discovers it via .mcp.json, and the `claude mcp add` alternative.

CONSTRAINTS:
- wiki_file_back is the compounding mechanism: enforce the threshold and tag digest pages so they are distinguishable from ingested facts.
- Do not invent tool registration syntax — confirm it against the installed SDK first.

ACCEPTANCE:
- tests/test_mcp.py: import the module and call the underlying tool functions directly (not over the wire) in stub mode — ingest, query, and file_back both above and below threshold — asserting behavior. Manual check: `python -m llmwiki.mcp_server` starts without error. `pytest -q` passes.

ON DONE: run tests, commit ("feat: MCP server exposing wiki tools"), report, and print the exact steps to connect from Claude Code.
```

---

## Prompt 6 — CLI + end-to-end demo

```
CONTEXT: Components are built and individually tested. Wire them into a CLI and prove the whole loop compounds.

OBJECTIVE: Implement src/llmwiki/cli.py and scripts/demo_end_to_end.py, plus an end-to-end test exercising ingest -> query -> file-back -> query.

BUILD:
- cli.py: subcommands ingest <file|->, query <text>, get <id>, file-back <question> <answer> [--score N], list, rebuild. Human-readable output. Wire the `wiki` console script to main().
- scripts/demo_end_to_end.py: runs in stub mode, end to end, on small bundled sample sources. Include one source containing a fake API key to demonstrate redaction. Steps, each printed: ingest source A and source B -> rebuild -> query -> synthesize and file_back a digest answer -> query again and show the digest now surfaces alongside the originals.
- tests/test_e2e.py: the programmatic version, asserting: both source pages created; the secret is redacted everywhere (page + saved source); search finds the right page; file_back above threshold creates a digest; a follow-up query retrieves that digest; git history contains the expected commits; and a fresh rebuild reproduces the search results.

CONSTRAINTS:
- The demo and the e2e test must run with NO network (WIKI_LLM_STUB=1).
- Keep sample data small and self-contained under tests/fixtures or scripts.

ACCEPTANCE:
- `python scripts/demo_end_to_end.py` prints the full loop. The whole suite `pytest -q` passes. `wiki query "..."` works from the shell.

ON DONE: run tests, commit ("feat: CLI and end-to-end demo of the compounding loop"), report a transcript of the demo run.
```

---

## Prompt 7 — Docs, runbook, scope boundaries (finalize)

```
CONTEXT: The PoC works end to end. Finalize so a new engineer can run it and clearly see what is deliberately out of scope.

OBJECTIVE: Produce the README runbook and a Makefile, and update CLAUDE.md with the final implemented schema and the deferred-phases map.

BUILD:
- Flesh out README.md: what it is; the architecture in one paragraph; prerequisites; setup; the four core commands; how to connect the MCP server to Claude Code; and a "Verify the PoC" checklist a reader can run top to bottom.
- A Makefile (or justfile) with targets: setup, test, demo, run-mcp, rebuild.
- Update CLAUDE.md: finalize the frontmatter schema as actually implemented; add an "Out of scope for this PoC / next phases" section mapping deferred features to the architecture roadmap — confidence scoring & decay and supersession lifecycle (Phase 2); entity extraction & typed-relationship graph (Phase 3); automation hooks & scheduler (Phase 4); vector stream + reciprocal rank fusion + quality scoring (Phase 5); multi-agent mesh sync & private/shared scoping (Phase 6).
- A short CONTRIBUTING note restating two rules: the canonical-vs-cache rule, and keep CLAUDE.md in sync with the code.

CONSTRAINTS:
- No new runtime features here — documentation and ergonomics only.
- The deferred-phases section must match the source architecture's roadmap.

ACCEPTANCE:
- On a fresh clone, `make setup && make test && make demo` works, and every item in the README "Verify the PoC" checklist passes.

ON DONE: commit ("docs: runbook, makefile, finalized schema and scope"), report the final tree and the checklist results.
```

---

## Verifying the PoC (after Prompt 7)

Run these yourself to confirm the loop:

1. `make setup` — installs the package and dev deps.
2. `make test` — full suite green, all in stub mode (no network).
3. `make demo` — prints: two sources ingested, a secret redacted, a query answered, an answer filed back as a digest, and a follow-up query surfacing that digest. That last step *is* the compounding behaviour.
4. `make run-mcp`, then connect from Claude Code and call `wiki_query` / `wiki_ingest` / `wiki_file_back` as tools.
5. Delete `wiki/.index/` and run `wiki rebuild` — search results are identical, proving the index is a pure cache of the Markdown.

If all five hold, you have a faithful end-to-end MVP and clean seams for Phases 2–6.

---

## Tips for getting the most from Claude Code on this build

- Keep **Opus 4.6** active for the whole build; the steps assume a single coherent agent context.
- One prompt per turn. Let it run the tests it wrote before you move on — the acceptance criteria are designed to be self-checking.
- Review each diff for the **canonical-vs-cache** invariant: nothing should be persisted only in SQLite.
- If a prompt's assumptions don't fit your environment (SDK versions especially), the prompts already instruct the agent to verify installed APIs — trust that over any version pinned here.
- Treat `CLAUDE.md` as the living spec. If you extend the PoC toward Phase 2+, update it first and let the agent follow it.
