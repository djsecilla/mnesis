"""Tests for the ingestion-boundary secret/PII filter.

The cardinal assertion throughout: a raw sensitive value must never appear in
the redacted text or the findings report.
"""

from __future__ import annotations

from mnesis.filters import scrub

# Fake, non-real values used only as fixtures.
FAKE_API_KEY = "sk-test1234567890ABCDEFGHijklmnop"
FAKE_EMAIL = "alice@example.com"
FAKE_PHONE = "+1 (555) 123-4567"


def _no_leak(value: str, redacted: str, findings: list[dict]) -> None:
    """Assert ``value`` leaks neither into the redacted text nor the findings."""
    assert value not in redacted
    assert value not in repr(findings)


def test_redacts_secret_pii_without_leaking():
    text = (
        f"Use key {FAKE_API_KEY} to authenticate. "
        f"Contact alice at {FAKE_EMAIL} or call {FAKE_PHONE}."
    )
    redacted, findings = scrub(text)

    # All three are redacted with the right labels.
    assert "[REDACTED:SECRET]" in redacted
    assert "[REDACTED:PII:email]" in redacted
    assert "[REDACTED:PII:phone]" in redacted

    # None of the raw values survive anywhere.
    for value in (FAKE_API_KEY, FAKE_EMAIL, FAKE_PHONE):
        _no_leak(value, redacted, findings)

    # Findings report types/kinds and spans only — never the value.
    kinds = {f["kind"] for f in findings}
    assert {"api-key", "email", "phone"} <= kinds
    for f in findings:
        assert set(f.keys()) == {"type", "kind", "start", "end"}
        assert f["type"] in {"secret", "pii"}
        assert isinstance(f["start"], int) and isinstance(f["end"], int)
        assert f["start"] < f["end"]


def test_clean_text_passes_through_unchanged():
    clean = (
        "Project Atlas uses Redis as its primary caching layer. "
        "Sarah owns the auth migration and reviews it weekly."
    )
    redacted, findings = scrub(clean)
    assert redacted == clean
    assert findings == []


def test_allowlist_suppresses_false_positive():
    text = f"Reach the on-call rotation at {FAKE_EMAIL}."
    redacted, findings = scrub(text, allowlist=[FAKE_EMAIL])
    assert redacted == text  # explicitly allowed, so left intact
    assert findings == []


def test_pem_block_and_aws_key_and_credit_card():
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIBdummykeymaterialthatisnotreal1234567890\n"
        "-----END RSA PRIVATE KEY-----"
    )
    aws = "AKIAIOSFODNN7EXAMPLE"
    card = "4111 1111 1111 1111"
    text = f"{pem}\nDeploy with {aws}.\nCard on file: {card}."
    redacted, findings = scrub(text)

    kinds = {f["kind"] for f in findings}
    assert "pem-private-key" in kinds
    assert "aws-access-key" in kinds
    assert "credit-card" in kinds

    for value in (pem, aws, card):
        _no_leak(value, redacted, findings)
    # The PEM key material specifically must be gone.
    assert "dummykeymaterial" not in redacted


def test_findings_offsets_are_within_original_text():
    text = f"key={FAKE_API_KEY}"
    _, findings = scrub(text)
    assert findings
    for f in findings:
        assert 0 <= f["start"] < f["end"] <= len(text)
        # The slice of the ORIGINAL text at this span is what got redacted.
        assert text[f["start"]:f["end"]]  # non-empty
