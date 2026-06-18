#!/usr/bin/env python3
"""Deterministic note normalizer for the parse-note skill.

Reads a notes InboundEvent as JSON (file path argument, or stdin) and emits a
clean ``{text, source_ref, skip, reason}``. It is a **pure text transform**: it
strips boilerplate and measures substance, and interprets NONE of the content's
semantics. That is the load-bearing security property — note text is DATA, so an
embedded directive ("ignore instructions", "mark pages stale", "set skip=false")
simply rides along in ``text`` and changes nothing about the output or behaviour.
This script calls no tools and never ingests.
"""
from __future__ import annotations

import json
import re
import sys

# Worth-ingesting thresholds: below either, the note is trivial and skipped.
_MIN_WORDS = 3
_MIN_CHARS = 12

# A leading YAML front-matter block: `---\n … \n---` at the very start.
_FRONT_MATTER = re.compile(r"\A﻿?---[ \t]*\n.*?\n---[ \t]*\n", re.DOTALL)
# HTML comments (non-greedy, across lines).
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
# Email-style signature delimiter line: `-- ` (the standard) on its own line.
_SIG_DELIM = re.compile(r"(?m)^-- ?$")
# "Sent from my iPhone/Android/…" trailers.
_SENT_FROM = re.compile(r"(?im)^[ \t]*sent from my .*$")
# Zero-width / BOM characters that add no substance.
_ZERO_WIDTH = re.compile(r"[​‌‍﻿]")


def _load() -> dict:
    raw = open(sys.argv[1], encoding="utf-8").read() if len(sys.argv) > 1 else sys.stdin.read()
    return json.loads(raw or "{}")


def _clean(text: str) -> str:
    """Strip front-matter / comments / signatures / boilerplate whitespace.

    Keeps the substantive content verbatim — this normalizes, it does not
    summarize, and it never acts on the content."""
    text = _FRONT_MATTER.sub("", text, count=1)
    text = _HTML_COMMENT.sub("", text)
    # Drop a trailing signature block (everything from the first `-- ` delimiter).
    text = _SIG_DELIM.split(text)[0]
    text = _SENT_FROM.sub("", text)
    text = _ZERO_WIDTH.sub("", text)
    # Normalize whitespace: rstrip each line, collapse 3+ blank lines, trim ends.
    lines = [ln.rstrip() for ln in text.splitlines()]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _resolve_source_ref(data: dict) -> str | None:
    ref = data.get("source_ref")
    if ref:
        return str(ref)
    rel = (data.get("metadata") or {}).get("rel_path")
    return f"note:{rel}" if rel else None


def main() -> int:
    data = _load()
    source_ref = _resolve_source_ref(data)
    raw_text = data.get("text")

    if not source_ref:
        out = {"skill": "parse-note", "source_ref": None, "text": "",
               "skip": True, "reason": "no source_ref (cannot establish provenance)"}
        print(json.dumps(out, indent=2))
        return 0

    cleaned = _clean(raw_text) if isinstance(raw_text, str) else ""
    words = len(cleaned.split())
    chars = len(cleaned)

    if chars == 0:
        skip, reason = True, "empty after cleaning"
    elif words < _MIN_WORDS or chars < _MIN_CHARS:
        skip, reason = True, f"trivial: {words} word(s), {chars} char(s) after cleaning"
    else:
        skip, reason = False, f"ok: {words} words, {chars} chars of substantive content"

    out = {
        "skill": "parse-note",
        "source_ref": source_ref,
        "text": "" if skip else cleaned,
        "skip": skip,
        "reason": reason,
    }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
