"""Anthropic client wrapper with a deterministic offline stub.

This is the only module that talks to the LLM. It centralizes the model name
(``config.MNESIS_LLM_MODEL``) and ``MAX_TOKENS`` so callers never hard-code them.

**Stub mode** (``config.MNESIS_LLM_STUB`` — set by ``MNESIS_LLM_STUB=1`` or the
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
      tags, relations}`` derived from the user text. Entities and relations come
      from ``tag{type:value}`` and ``rel{s|p|o}`` markers in the source, so tests
      can drive entity/edge extraction offline.
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
    tags = re.findall(r"tag\{([^}]*)\}", text)
    relations = []
    for m in re.findall(r"rel\{([^}]*)\}", text):
        parts = [p.strip() for p in m.split("|")]
        if len(parts) == 3:
            relations.append({"s": parts[0], "p": parts[1], "o": parts[2]})
    payload = {
        "title": title,
        "summary_markdown": summary,
        "key_facts": key_facts,
        "tags": tags,
        "relations": relations,
    }
    return json.dumps(payload)


def _anthropic_complete(system: str, user: str) -> str:
    global _client
    if _client is None:
        import anthropic

        _client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
    response = _client.messages.create(
        model=config.MNESIS_LLM_MODEL,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    )


def _local_complete(system: str, user: str) -> str:
    """Call a local Ollama / OpenAI-compatible chat endpoint — no external calls.

    Targets ``{MNESIS_LLM_BASE_URL}/v1/chat/completions`` with ``MNESIS_LLM_MODEL``.
    No API key: inference stays on the host, inside the trust boundary.
    """
    import httpx

    url = config.MNESIS_LLM_BASE_URL.rstrip("/") + "/v1/chat/completions"
    response = httpx.post(
        url,
        json={
            "model": config.MNESIS_LLM_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "stream": False,
        },
        timeout=config.MNESIS_LLM_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]


def _factory_complete(system: str, user: str) -> str:
    """Broader providers (openai/google/mistral/bedrock/ollama/openai_compatible)
    via the shared multi-LLM factory — so Mnesis runs on any configured provider.

    Lazy: langchain is only needed on this path. Missing deps raise a clear,
    actionable error (install the matching extra). The native anthropic/local and
    stub paths are unchanged, so existing behaviour and offline tests are intact.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    from mnesis_llm.factory import get_chat_model

    model = get_chat_model(
        config.MNESIS_LLM_PROVIDER,
        config.MNESIS_LLM_MODEL or "",
        base_url=config.MNESIS_LLM_BASE_URL,
        api_key=config.MNESIS_LLM_API_KEY,
        temperature=config.MNESIS_LLM_TEMPERATURE,
    )
    resp = model.invoke([SystemMessage(content=system), HumanMessage(content=user)])
    return resp.content if isinstance(resp.content, str) else str(resp.content)


def complete(system: str, user: str) -> str:
    """Return the model's text response to ``system``/``user``.

    Routed at call time (so tests can toggle): the offline **stub** when
    ``config.MNESIS_LLM_STUB`` is set; the native **local** (OpenAI-compatible
    httpx) and **anthropic** (SDK) paths unchanged; any **other** provider goes
    through the shared multi-LLM factory (``openai``/``google``/``mistral``/
    ``bedrock``/``ollama``/``openai_compatible``).
    """
    if config.MNESIS_LLM_STUB:
        return _stub_complete(system, user)
    provider = config.MNESIS_LLM_PROVIDER
    if provider == "local":
        return _local_complete(system, user)
    if provider == "anthropic":
        return _anthropic_complete(system, user)
    return _factory_complete(system, user)
