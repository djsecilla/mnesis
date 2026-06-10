"""Secret / PII redaction at the ingestion boundary (CLAUDE.md §2.2, §12).

This is the filter that runs *before* anything is persisted, logged, or sent to
the LLM. Its contract is non-negotiable: a raw secret or PII value must never
survive into the redacted text, the findings report, or any return value.

Design tradeoff — **conservative by default**: for the PoC we prefer
over-redaction (a false positive that masks a harmless token) over leakage (a
false negative that lets a secret through). The detectors are deliberately
simple: regexes for common secret/PII shapes plus a Shannon-entropy heuristic
for unstructured high-entropy blobs. This is intentionally a floor, not a
ceiling — the production upgrade path is `detect-secrets` (Yelp) for secret
scanning and Microsoft Presidio for PII, which bring ML/NER and a far larger
recognizer set.

All functions here are pure: no file, network, or logging I/O.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

# Replacement tokens. Secrets collapse to a single label; PII keeps its subtype
# so a redacted document still reads sensibly (CLAUDE.md placeholders).
_SECRET_TOKEN = "[REDACTED:SECRET]"


def _pii_token(kind: str) -> str:
    return f"[REDACTED:PII:{kind}]"


# Entropy gate for the heuristic detectors (bits/char). Random base64 ~6,
# random hex ~4; ordinary words/identifiers fall well below this.
_ENTROPY_THRESHOLD = 3.5


@dataclass(frozen=True)
class _Detector:
    """One detection rule. ``group`` selects the sensitive sub-span to redact
    (e.g. the token *after* ``Bearer``). Higher ``priority`` wins on overlap."""

    type: str  # "secret" | "pii"
    kind: str  # specific shape, e.g. "api-key", "email"
    regex: re.Pattern[str]
    priority: int
    group: int = 0
    entropy_gated: bool = False


# Order here is documentary; overlap resolution uses ``priority``, not position.
_DETECTORS: tuple[_Detector, ...] = (
    # --- Secrets ---
    _Detector(
        "secret",
        "pem-private-key",
        re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"),
        priority=100,
    ),
    _Detector(
        "secret",
        "aws-access-key",
        re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA)[0-9A-Z]{16}\b"),
        priority=90,
    ),
    _Detector(
        "secret",
        "api-key",
        # sk-/rk-/pk- style keys (OpenAI, Stripe, etc.).
        re.compile(r"\b[a-z]{2,4}-[A-Za-z0-9_\-]{16,}\b"),
        priority=90,
    ),
    _Detector(
        "secret",
        "bearer-token",
        re.compile(r"(?i)\bbearer\s+([A-Za-z0-9._\-]{8,})"),
        priority=85,
        group=1,  # redact only the token, keep the "Bearer " prefix readable
    ),
    # --- PII ---
    _Detector(
        "pii",
        "email",
        re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
        priority=80,
    ),
    _Detector(
        "pii",
        "credit-card",
        # 13–19 digits, optionally single-space/hyphen grouped.
        re.compile(r"(?<!\d)(?:\d[ \-]?){12,18}\d(?!\d)"),
        priority=70,
    ),
    _Detector(
        "pii",
        "phone",
        re.compile(
            r"(?<!\w)(?:\+?\d{1,2}[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}(?!\w)"
        ),
        priority=60,
    ),
    # --- High-entropy blobs (entropy-gated to limit false positives) ---
    _Detector(
        "secret",
        "hex-blob",
        re.compile(r"\b[0-9a-fA-F]{32,}\b"),
        priority=40,
        entropy_gated=True,
    ),
    _Detector(
        "secret",
        "base64-blob",
        re.compile(r"\b[A-Za-z0-9+/]{24,}={0,2}"),
        priority=35,
        entropy_gated=True,
    ),
    _Detector(
        "secret",
        "high-entropy",
        # Generic unstructured token; require a digit so plain words don't trip.
        re.compile(r"\b(?=[A-Za-z0-9_\-]*\d)[A-Za-z0-9_\-]{20,}\b"),
        priority=30,
        entropy_gated=True,
    ),
)


def _entropy(s: str) -> float:
    """Shannon entropy of ``s`` in bits per character."""
    if not s:
        return 0.0
    counts = Counter(s)
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


@dataclass
class _Match:
    type: str
    kind: str
    start: int
    end: int
    priority: int
    token: str


def scrub(text: str, allowlist: list[str] | None = None) -> tuple[str, list[dict]]:
    """Redact secrets and PII from ``text``.

    Returns ``(redacted_text, findings)`` where ``findings`` is a list of
    ``{type, kind, start, end}`` dicts (original-text offsets). The matched value
    is *never* included anywhere in the return value — only its type, kind, and
    span. ``allowlist`` suppresses exact-string false positives.
    """
    allowed = set(allowlist or ())

    # 1. Collect every candidate span across all detectors.
    candidates: list[_Match] = []
    for det in _DETECTORS:
        for m in det.regex.finditer(text):
            value = m.group(det.group)
            if not value:
                continue
            if value in allowed:
                continue
            if det.entropy_gated and _entropy(value) < _ENTROPY_THRESHOLD:
                continue
            start, end = m.span(det.group)
            token = _SECRET_TOKEN if det.type == "secret" else _pii_token(det.kind)
            candidates.append(_Match(det.type, det.kind, start, end, det.priority, token))

    # 2. Resolve overlaps: greedily keep the highest-priority (then longest,
    #    then earliest) match, dropping anything that overlaps an accepted span.
    candidates.sort(key=lambda c: (-c.priority, -(c.end - c.start), c.start))
    accepted: list[_Match] = []
    for c in candidates:
        if any(c.start < a.end and a.start < c.end for a in accepted):
            continue
        accepted.append(c)

    # 3. Stitch the redacted text from non-overlapping spans, left to right.
    accepted.sort(key=lambda c: c.start)
    out: list[str] = []
    cursor = 0
    for a in accepted:
        out.append(text[cursor:a.start])
        out.append(a.token)
        cursor = a.end
    out.append(text[cursor:])
    redacted = "".join(out)

    findings = [
        {"type": a.type, "kind": a.kind, "start": a.start, "end": a.end}
        for a in accepted
    ]
    return redacted, findings
