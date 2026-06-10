"""Anthropic client wrapper with a deterministic offline stub.

This is the only module that talks to the LLM. It centralizes the model name
(``config.WIKI_LLM_MODEL``) and ``MAX_TOKENS`` so callers never hard-code them.

**Stub mode** (``config.WIKI_LLM_STUB`` — set by ``WIKI_LLM_STUB=1`` or the
absence of an API key) returns a deterministic, network-free JSON response
derived from the prompt, so the test suite and the demo run fully offline.

Matched against the installed ``anthropic`` SDK (0.109.x) Messages API:
``client.messages.create(model=, max_tokens=, system=, messages=[...])`` with
text returned in ``response.content[i].text``.
"""

from __future__ import annotations

import json
import re

from . import config

#: Upper bound on extraction output. Centralized here, not at call sites.
MAX_TOKENS = 1024

_client = None  # lazily constructed real SDK client (never in stub mode)


_RELATION_LABELS = ("reinforces", "supersedes", "contradicts", "unrelated")


def _stub_complete(system: str, user: str) -> str:
    """Deterministic canned JSON without any network call.

    Two request shapes are recognized by their system prompt:

    - **Relation classification** (the prompt names all four relation labels):
      return ``{"label", "justification"}``, the label read from a
      ``relation:<label>`` marker in the user text (default ``unrelated``). This
      lets tests drive every ingest branch deterministically.
    - **Extraction** (default): return ``{title, summary_markdown, key_facts,
      tags}`` derived from the user text.
    """
    if all(label in system for label in _RELATION_LABELS):
        match = re.search(r"relation:(reinforces|supersedes|contradicts|unrelated)", user)
        label = match.group(1) if match else "unrelated"
        return json.dumps(
            {"label": label, "justification": "stub: deterministic from fixture marker"}
        )

    text = user.strip()
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    # Title from the first sentence (declarative-ish), not the whole blob.
    first = sentences[0] if sentences else "Untitled source"
    title = first[:80].rstrip()
    summary = " ".join(text.split())[:300]
    key_facts = sentences[:3] or [title]
    payload = {
        "title": title,
        "summary_markdown": summary,
        "key_facts": key_facts,
        "tags": [],
    }
    return json.dumps(payload)


def _real_complete(system: str, user: str) -> str:
    global _client
    if _client is None:
        import anthropic

        _client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
    response = _client.messages.create(
        model=config.WIKI_LLM_MODEL,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    )


def complete(system: str, user: str) -> str:
    """Return the model's text response to ``system``/``user``.

    Routes to the offline stub whenever ``config.WIKI_LLM_STUB`` is set
    (read at call time so tests can toggle it).
    """
    if config.WIKI_LLM_STUB:
        return _stub_complete(system, user)
    return _real_complete(system, user)
