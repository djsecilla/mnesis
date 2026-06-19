"""EmailSendChannel — the first external channel (E2), behind the E1 plane.

This is the first ``risk_class=external`` channel. It is built to be **safe by
construction**:

  - **Dry-run by default** (`MNESIS_EMAIL_DRYRUN`, default true) — it renders the
    exact message + recipient + endpoint and **sends nothing**. A live send
    requires dry-run explicitly off.
  - **Behind the egress control plane (E1)** — `check_send_allowed` runs against
    the validated recipient + endpoint immediately before any send; on deny, it
    does not send.
  - **Payload secret-scan** — the *final rendered message* is scanned for
    secrets/PII; on a hit it **blocks** (defense in depth beyond Mnesis's
    ingest-time redaction). A block flags for review, never sends.
  - **At-most-once** — an idempotency key (per proposal) prevents a re-send; an
    **ambiguous** transport failure is reported as ``needs_human`` and is
    **never auto-retried** (it records the key so it can't re-send).
  - **TLS required; credentials from env/secret store** — never in code or the
    image; the endpoint must be on the egress allowlist.

The `DeliveryResult` records ``status`` (``dry_run``/``sent``/``blocked``/
``failed``/``needs_human``), the recipient, the endpoint, and a content hash —
**never the body or any secret**.
"""
from __future__ import annotations

import hashlib
import json
import logging
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable

from . import config, secret_scan
from .channels import RISK_EXTERNAL, DeliveryResult, OutboundArtifact, OutboundChannel
from .egress import EgressPolicy, Recipient
from .triggers.connector import path_lock

log = logging.getLogger("mnesis_agents.email")

#: SMTP failures *after* the message may have been accepted — delivery is
#: AMBIGUOUS, so we must not auto-retry (at-most-once → surface for a human).
_AMBIGUOUS_EXC: tuple[type[Exception], ...] = (
    smtplib.SMTPServerDisconnected,
    smtplib.SMTPResponseException,
    TimeoutError,
    ConnectionResetError,
    BrokenPipeError,
)


class AmbiguousSendError(Exception):
    """Raised by a transport when it cannot tell whether the message was sent."""


# ── At-most-once ledger ─────────────────────────────────────────────────────


class _SentStore:
    """Durable set of idempotency keys already sent (or attempted ambiguously), so
    a send happens **at most once**. JSON, lock-guarded."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = path_lock(self.path)

    def contains(self, key: str) -> bool:
        with self._lock:
            if not self.path.is_file():
                return False
            try:
                return key in set(json.loads(self.path.read_text(encoding="utf-8") or "[]"))
            except Exception:  # noqa: BLE001
                return False

    def add(self, key: str) -> None:
        with self._lock:
            keys = []
            if self.path.is_file():
                try:
                    keys = json.loads(self.path.read_text(encoding="utf-8") or "[]")
                except Exception:  # noqa: BLE001
                    keys = []
            if key not in keys:
                keys.append(key)
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(self.path.suffix + ".tmp")
                tmp.write_text(json.dumps(keys), encoding="utf-8")
                tmp.replace(self.path)


# ── Default SMTP/TLS transport ──────────────────────────────────────────────


def _smtp_transport(
    *, host: str, port: int, username: str | None, password: str | None,
    starttls: bool, sender: str, recipient: str, message: str, timeout: float,
) -> None:
    """Send one message over SMTP + STARTTLS. Raises :class:`AmbiguousSendError`
    when a failure leaves delivery uncertain (so the caller won't retry)."""
    import ssl

    try:
        with smtplib.SMTP(host, port, timeout=timeout) as smtp:
            if starttls:
                smtp.starttls(context=ssl.create_default_context())
            if username:
                smtp.login(username, password or "")
            smtp.sendmail(sender, [recipient], message)
    except _AMBIGUOUS_EXC as exc:
        raise AmbiguousSendError(str(exc)) from exc


#: A transport: keyword-only, raises on failure (AmbiguousSendError if uncertain).
Transport = Callable[..., None]


# ── The channel ─────────────────────────────────────────────────────────────


class EmailSendChannel(OutboundChannel):
    """Send a brief as an email — external, dry-run by default, gated by E1."""

    name = "email"
    risk_class = RISK_EXTERNAL

    def __init__(
        self,
        *,
        egress: EgressPolicy | None = None,
        dryrun: bool | None = None,
        sender: str | None = None,
        host: str | None = None,
        port: int | None = None,
        username: str | None = None,
        password: str | None = None,
        starttls: bool | None = None,
        timeout: float | None = None,
        transport: Transport | None = None,
        sent_store: _SentStore | None = None,
        scanner: Callable[[str], list[str]] | None = None,
    ) -> None:
        self._egress = egress if egress is not None else EgressPolicy.from_config()
        self._dryrun = config.MNESIS_EMAIL_DRYRUN if dryrun is None else dryrun
        self._sender = sender if sender is not None else config.MNESIS_EMAIL_FROM
        self._host = host if host is not None else config.MNESIS_SMTP_HOST
        self._port = port if port is not None else config.MNESIS_SMTP_PORT
        self._username = username if username is not None else config.MNESIS_SMTP_USERNAME
        self._password = password if password is not None else config.MNESIS_SMTP_PASSWORD
        self._starttls = config.MNESIS_EMAIL_STARTTLS if starttls is None else starttls
        self._timeout = config.MNESIS_EMAIL_TIMEOUT if timeout is None else timeout
        self._transport = transport or _smtp_transport
        self._sent = sent_store if sent_store is not None else _SentStore(
            config.MNESIS_EGRESS_STATE_DIR / "email_sent.json"
        )
        self._scan = scanner or secret_scan.scan

    # -- helpers ---------------------------------------------------------------

    def _endpoint(self) -> str:
        return f"{self._host}:{self._port}" if self._host else ""

    def endpoint(self) -> str | None:
        return self._endpoint() or None

    def preview(self, artifact, destination=None, context=None):
        """A dry-run preview: the exact rendered message + recipient + endpoint +
        payload-scan findings — for the human at the gate. Sends nothing."""
        from .channels import ChannelPreview

        recipient = (destination or "").strip()
        message = self._render(artifact, recipient)
        content_hash = "sha256:" + hashlib.sha256(message.encode("utf-8")).hexdigest()
        findings = self._scan(f"{artifact.title or ''}\n{artifact.body or ''}\n{message}")
        return ChannelPreview(
            channel=self.name, risk_class=self.risk_class, recipient=recipient,
            endpoint=self._endpoint(), subject=artifact.title or "", body=artifact.body or "",
            content_hash=content_hash, secret_findings=findings,
        )

    def _render(self, artifact: OutboundArtifact, recipient: str) -> str:
        msg = EmailMessage()
        msg["From"] = self._sender or "(unset-sender)"
        msg["To"] = recipient
        msg["Subject"] = artifact.title or artifact.kind or "(no subject)"
        msg.set_content(artifact.body or "")
        return msg.as_string()

    def _result(self, status: str, *, recipient, endpoint, content_hash, detail="", error=None) -> DeliveryResult:
        return DeliveryResult(
            channel=self.name, risk_class=self.risk_class, status=status,
            destination=recipient, recipient=recipient, endpoint=endpoint,
            content_hash=content_hash, detail=detail, error=error,
        )

    # -- deliver ---------------------------------------------------------------

    def deliver(
        self, artifact: OutboundArtifact, destination: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> DeliveryResult:
        context = context or {}
        recipient = (destination or "").strip()
        # The recipient SOURCE must be supplied by the caller (the gate passes
        # policy/user); absent → unknown → the egress plane fails closed.
        source = context.get("recipient_source") or "unknown"
        idem = context.get("idempotency_key") or context.get("proposal_id")
        endpoint = self._endpoint()
        now = context.get("now") or datetime.now(timezone.utc)

        try:
            message = self._render(artifact, recipient)
            content_hash = "sha256:" + hashlib.sha256(message.encode("utf-8")).hexdigest()

            def res(status, **kw):
                return self._result(status, recipient=recipient, endpoint=endpoint,
                                    content_hash=content_hash, **kw)

            # At-most-once: this idempotency key already sent (or ambiguously
            # attempted) → never send again.
            if idem and self._sent.contains(str(idem)):
                return res("sent", detail="already sent for this idempotency key (no re-send)")

            # Egress decision (computed once; enforced for any live send).
            decision = self._egress.check_send_allowed(
                RISK_EXTERNAL, Recipient(recipient, source), endpoint, now=now)

            # Payload secret-scan ALWAYS (defense in depth) — block, dry-run or live.
            # Scan the PLAINTEXT payload (subject + body) as well as the rendered
            # message, so a transfer-encoding (quoted-printable/base64) can't hide a
            # secret the recipient would simply decode.
            scan_text = f"{artifact.title or ''}\n{artifact.body or ''}\n{message}"
            findings = self._scan(scan_text)
            if findings:
                log.warning("email send BLOCKED by secret-scan (%d finding(s))", len(findings))
                return res("blocked", error="payload secret-scan hit",
                           detail=f"blocked: payload secret-scan found {findings} — flagged for review, not sent")

            # Dry-run (default): render + return; send NOTHING. Surface the egress
            # decision so the operator sees whether a live send WOULD be allowed.
            if self._dryrun:
                verdict = "would ALLOW" if decision.allowed else f"would DENY ({decision.reason})"
                return res("dry_run", detail=f"DRY-RUN: rendered, not sent; egress {verdict}")

            # --- live send path ---
            if decision.denied:
                return res("blocked", error="egress denied",
                           detail=f"blocked by egress control plane: {decision.reason}")
            if not self._starttls:
                return res("blocked", error="tls required",
                           detail="blocked: TLS (STARTTLS) is required for a live send")
            if not (self._host and self._sender):
                return res("failed", error="misconfigured",
                           detail="blocked: SMTP host and sender (From) are required for a live send")

            try:
                self._transport(
                    host=self._host, port=self._port, username=self._username,
                    password=self._password, starttls=self._starttls,
                    sender=self._sender, recipient=recipient, message=message,
                    timeout=self._timeout,
                )
            except AmbiguousSendError as exc:
                # Delivery is uncertain → at-most-once means DO NOT retry. Record the
                # key so a re-trigger can't re-send, and flag for a human to verify.
                if idem:
                    self._sent.add(str(idem))
                log.warning("email send AMBIGUOUS — flagged needs_human, not retried")
                return res("needs_human",
                           detail="ambiguous transport failure after a send attempt; "
                                  "NOT auto-retried — a human must verify delivery", error=str(exc))
            except Exception as exc:  # noqa: BLE001 — a clean failure (definitely not sent)
                log.warning("email send failed (clean, not sent): %s", exc)
                return res("failed", detail="send failed (not delivered)", error=str(exc))

            # Clean success → record idempotency key (at-most-once) and report sent.
            if idem:
                self._sent.add(str(idem))
            log.info("email sent to %s via %s", recipient.split("@")[-1] if "@" in recipient else "?", endpoint)
            return res("sent", detail="sent via SMTP+TLS")
        except Exception as exc:  # noqa: BLE001 — a channel reports, never crashes
            return self._result("failed", recipient=recipient, endpoint=endpoint,
                                content_hash=None, detail="unexpected error", error=str(exc))
