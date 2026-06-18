#!/usr/bin/env python3
"""Deterministic brief composer for the prepare-meeting-brief skill.

Reads the gathered meeting context + Mnesis read-tool results as JSON (file path
argument, or stdin) and composes a grounded, cited ``{title, markdown, citations,
suggested_channel}`` artifact. It is a **pure transform**: it quotes page
titles/snippets as DATA and interprets none of their semantics. The load-bearing
properties are structural —

  * it emits **no destination** (the destination is the operator's choice at the
    gate, never derived from content/context);
  * ``suggested_channel`` is a **fixed safe default** (the inert ``draft-outbox``),
    independent of all input;
  * it **cites only real page ids** (those the read tools returned), and when the
    knowledge is thin it **says so** rather than confabulating;
  * it calls no tools and performs no delivery.
"""
from __future__ import annotations

import json
import sys

#: Always the inert draft channel — NEVER derived from content or context.
SUGGESTED_CHANNEL = "draft-outbox"

_MAX_POINTS = 8
_MAX_ENTITIES = 6


def _load() -> dict:
    raw = open(sys.argv[1], encoding="utf-8").read() if len(sys.argv) > 1 else sys.stdin.read()
    return json.loads(raw or "{}")


def _clean(text: object) -> str:
    return " ".join(str(text or "").split())


def main() -> int:
    data = _load()
    context = data.get("context") or {}
    topic = _clean(context.get("topic")) or "(untitled topic)"
    attendees = [str(a) for a in (context.get("attendees") or [])]
    time = _clean(context.get("time"))

    hits = [h for h in (data.get("hits") or []) if isinstance(h, dict)]
    contradictions = set(data.get("contradictions") or [])
    for h in hits:
        if h.get("contradicted"):
            contradictions.add(h.get("id"))
    entities = [e for e in (data.get("entities") or []) if isinstance(e, dict)]
    impact = [a for a in (data.get("impact") or []) if isinstance(a, dict)]

    # Active, grounded pages drive the key points.
    active = [h for h in hits if h.get("status", "active") != "stale"][:_MAX_POINTS]
    cited: list[str] = []

    lines = [f"# Meeting brief: {topic}", ""]
    meta = []
    if attendees:
        meta.append("**Attendees:** " + ", ".join(attendees))
    if time:
        meta.append("**When:** " + time)
    if meta:
        lines += ["  ".join(meta), ""]

    # Key points (grounded, cited) — or an honest "thin knowledge" note.
    lines.append("## Key points")
    if active:
        for h in active:
            hid = h.get("id")
            title = _clean(h.get("title")) or "(untitled page)"
            snippet = _clean(h.get("snippet"))
            flag = "  ⚠ (contradiction under review)" if hid in contradictions else ""
            tail = f" — {snippet}" if snippet else ""
            lines.append(f"- **{title}**{tail} [{hid}]{flag}")
            if hid:
                cited.append(hid)
    else:
        lines.append(
            f'_Mnesis has little on "{topic}" — no relevant pages were found. '
            "This brief is **not grounded** in stored knowledge; gather context "
            "independently before the meeting._"
        )
    lines.append("")

    # Open contradictions to be aware of.
    contra = [h for h in hits if h.get("id") in contradictions]
    lines.append("## Open contradictions to be aware of")
    if contra:
        for h in contra:
            hid = h.get("id")
            lines.append(
                f"- ⚠ **{_clean(h.get('title')) or hid}** [{hid}] — under review; treat as unsettled"
            )
            if hid:
                cited.append(hid)
    else:
        lines.append("- None flagged.")
    lines.append("")

    # Related entities & impact (optional, from the graph read tools).
    if entities or impact:
        lines.append("## Related entities & impact")
        for e in entities[:_MAX_ENTITIES]:
            lines.append(f"- `{_clean(e.get('ref'))}` ({_clean(e.get('type')) or '?'})")
        for a in impact[:_MAX_ENTITIES]:
            path = " → ".join(str(p) for p in (a.get("path") or []))
            lines.append(f"- impact: {path} (via {_clean(a.get('predicate')) or '?'})")
        lines.append("")

    citations = sorted({c for c in cited if c})
    lines.append("## Sources")
    if citations:
        lines += [f"- {c}" for c in citations]
    else:
        lines.append("- (none — this brief is not grounded in stored pages)")

    out = {
        "skill": "prepare-meeting-brief",
        "title": f"Meeting brief: {topic}",
        "markdown": "\n".join(lines).strip() + "\n",
        "citations": citations,
        "suggested_channel": SUGGESTED_CHANNEL,   # fixed; never content/context-derived
        "thin_knowledge": not active,
    }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
