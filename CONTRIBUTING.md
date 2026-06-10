# Contributing to mnesis

Two rules matter more than any style guideline. Internalize them before
changing anything.

## 1. Markdown is canonical; the index is a rebuildable cache

The Markdown pages under `wiki/pages/` (and the redacted sources under
`wiki/sources/`) are the **single source of truth**, versioned in git. The
SQLite search index under `wiki/.index/` is a **pure projection** of those pages
— `mnesis rebuild` must be able to reconstruct it identically from Markdown
alone. **Never** persist anything in the index (or any cache) that is not
derivable from a page. If you find yourself wanting to store state only in the
index, it belongs in the Markdown instead.

## 2. Keep `CLAUDE.md` in sync with the code

[`CLAUDE.md`](CLAUDE.md) is the operating contract: the schema, conventions, and
scope of the system. Any code change that touches a frontmatter field, a
directory, an env var, a tool, or a documented behaviour **must update
`CLAUDE.md` in the same commit**. When the file and the code disagree, treat
`CLAUDE.md` as the intended design and the code as the bug. Extending the PoC
toward a later phase? Update `CLAUDE.md` *first*, then make the code follow it.

---

Practical notes: use `uv` for the environment (`make setup`); run `make test`
(offline, no API key needed) before committing; every page mutation is one git
commit (don't batch unrelated writes, don't rewrite history).
