---
name: summarize-source
description: Summarize a source document into a concise, factual digest. Use when the user asks for a summary of provided text or of a Mnesis source page.
version: 0.1.0
license: MIT
allowed-tools:
  - mnesis_get
  - mnesis_query
---
# Summarize Source (EXAMPLE skill)

> ⚠️ Sample skill bundled with mnesis_agents to validate skill discovery and
> activation end to end. It is intentionally trivial and is **not** a real
> domain skill.

When asked to summarize a source:

1. If you were given a Mnesis page id, fetch the page first with `mnesis_get`.
2. Write a **declarative** one-sentence statement of the source's main claim,
   followed by 1–2 sentences of supporting detail. State only what the source
   supports; do not speculate.
3. Follow the style guide in `references/style.md`.

Optionally, you may run `scripts/wordcount.py <file>` to check a draft's length
(a summary should stay short).
