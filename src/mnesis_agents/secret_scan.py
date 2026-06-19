"""A focused secret/PII scanner for **outbound payloads** — defense in depth.

This is independent of Mnesis's own ingest-time redaction (the agents layer never
imports ``mnesis``): it is a last-line check an external channel runs on the
*final rendered message* before sending, so a secret that somehow reached an
artifact can never leave the box. It is deliberately tuned for **high-confidence
secret patterns** (keys, tokens, private keys, credentials, obvious PII) to avoid
false-positives on ordinary brief prose (a page that merely mentions a person or
an email is *not* flagged).

:func:`scan` returns the **categories** of any findings — **never the matched
values** — so callers can block-and-flag without ever logging the secret.
"""
from __future__ import annotations

import re

# (category, compiled pattern). High-confidence only.
_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    (
        "credential_assignment",
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret(?:[_-]?key)?|access[_-]?token|client[_-]?secret|"
            r"password|passwd|pwd|auth[_-]?token|bearer)\b\s*[:=]\s*['\"]?[A-Za-z0-9._\-/+=]{8,}"
        ),
    ),
]

# A run of 13–19 digits (spaces/dashes allowed) — Luhn-validated to a card number.
_CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")


def _luhn_ok(digits: str) -> bool:
    nums = [int(c) for c in digits if c.isdigit()]
    if not 13 <= len(nums) <= 19:
        return False
    total, parity = 0, len(nums) % 2
    for i, n in enumerate(nums):
        if i % 2 == parity:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def scan(text: str | None) -> list[str]:
    """Return the sorted, de-duplicated **categories** of secret/PII findings in
    ``text`` (empty list = clean). Never returns or logs the matched values."""
    if not text:
        return []
    found: set[str] = set()
    for category, pattern in _PATTERNS:
        if pattern.search(text):
            found.add(category)
    for m in _CARD_RE.finditer(text):
        if _luhn_ok(m.group(0)):
            found.add("credit_card")
            break
    return sorted(found)


def has_secret(text: str | None) -> bool:
    return bool(scan(text))
