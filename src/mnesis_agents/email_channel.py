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
from .send_audit import SendAuditLog
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


# ── At-most-once idempotency ledger (crash-safe state machine) ──────────────
# Per send key (a stable id per approved proposal): in_flight | sent | needs_human.
# The key is marked ``in_flight`` BEFORE transmit, so a process crash mid-send
# leaves it ``in_flight`` — which a duplicate path resolves to **needs_human**
# (never an automatic resend).

_IN_FLIGHT = "in_flight"
_SENT = "sent"
_NEEDS_HUMAN = "needs_human"


class _SentStore:
    """Durable ``{send_key: state}`` ledger for at-most-once delivery. JSON,
    lock-guarded, atomic writes."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = path_lock(self.path)

    def _load(self) -> dict[str, str]:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8") or "{}")
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def state(self, key: str) -> str | None:
        with self._lock:
            return self._load().get(key)

    def set(self, key: str, state: str) -> None:
        with self._lock:
            data = self._load()
            data[key] = state
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(self.path)

    def delete(self, key: str) -> None:
        with self._lock:
            data = self._load()
            if data.pop(key, None) is not None:
                tmp = self.path.with_suffix(self.path.suffix + ".tmp")
                tmp.write_text(json.dumps(data), encoding="utf-8")
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
        send_audit: SendAuditLog | None = None,
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
        self._send_audit = send_audit if send_audit is not None else SendAuditLog()

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
        # The stable send key per approved proposal (at-most-once across the path).
        key = str(context.get("idempotency_key") or context.get("proposal_id") or "")
        approval_id = context.get("approval_id")
        endpoint = self._endpoint()
        now = context.get("now") or datetime.now(timezone.utc)

        content_hash = None
        try:
            message = self._render(artifact, recipient)
            content_hash = "sha256:" + hashlib.sha256(message.encode("utf-8")).hexdigest()

            def finish(status, *, decision, detail="", error=None) -> DeliveryResult:
                """Write exactly ONE immutable send-audit record, then return."""
                try:
                    self._send_audit.record(
                        proposal_id=context.get("proposal_id"), approval_id=approval_id,
                        channel=self.name, recipient=recipient, endpoint=endpoint,
                        content_hash=content_hash, decision=decision, status=status,
                    )
                except Exception:  # noqa: BLE001 — auditing never breaks delivery
                    log.exception("send-audit write failed")
                return self._result(status, recipient=recipient, endpoint=endpoint,
                                    content_hash=content_hash, detail=detail, error=error)

            # 1) At-most-once: resolve an already-seen send key BEFORE anything.
            state = self._sent.state(key) if key else None
            if state == _SENT:
                return finish("sent", decision="idempotent", detail="already sent (no re-send)")
            if state in (_IN_FLIGHT, _NEEDS_HUMAN):
                # A prior attempt's outcome is unknown (possible crash mid-send) →
                # resolve to needs_human; NEVER an automatic resend.
                return finish("needs_human", decision="idempotent",
                              detail="a prior send attempt is unresolved (possible crash); "
                                     "NOT resent — a human must verify delivery")

            # 2) Payload secret-scan ALWAYS (defense in depth) — over plaintext +
            #    rendered, so a transfer-encoding can't hide a secret.
            findings = self._scan(f"{artifact.title or ''}\n{artifact.body or ''}\n{message}")
            if findings:
                log.warning("email send BLOCKED by secret-scan (%d finding(s))", len(findings))
                return finish("blocked", decision="secret_scan", error="payload secret-scan hit",
                              detail=f"blocked: payload secret-scan found {findings} — flagged, not sent")

            # 3) Dry-run (default): render + return; send NOTHING.
            if self._dryrun:
                preview = self._egress.check_send_allowed(
                    RISK_EXTERNAL, Recipient(recipient, source), endpoint, now=now)
                verdict = "would ALLOW" if preview.allowed else f"would DENY ({preview.reason})"
                return finish("dry_run", decision=f"dry_run/{verdict}",
                              detail=f"DRY-RUN: rendered, not sent; egress {verdict}")

            # --- live send path ---
            if not self._starttls:
                return finish("blocked", decision="tls_required", error="tls required",
                              detail="blocked: TLS (STARTTLS) is required for a live send")
            if not (self._host and self._sender):
                return finish("failed", decision="misconfigured", error="misconfigured",
                              detail="blocked: SMTP host and sender (From) are required")

            # 4) E1 at the LAST moment before transmit: kill-switch + quota +
            #    allowlist + endpoint, re-evaluated NOW (a kill/quota change after
            #    approval still halts the send).
            decision = self._egress.check_send_allowed(
                RISK_EXTERNAL, Recipient(recipient, source), endpoint, now=now)
            if decision.denied:
                return finish("blocked", decision=f"egress_deny/{decision.reason}",
                              error="egress denied", detail=f"blocked by egress: {decision.reason}")

            # 5) Commit: count the send against quotas, then mark the key IN-FLIGHT
            #    BEFORE transmit — a crash now leaves it in_flight → needs_human.
            self._egress.record_send(Recipient(recipient, "policy"), now=now)
            if key:
                self._sent.set(key, _IN_FLIGHT)

            try:
                self._transport(
                    host=self._host, port=self._port, username=self._username,
                    password=self._password, starttls=self._starttls,
                    sender=self._sender, recipient=recipient, message=message,
                    timeout=self._timeout,
                )
            # NOTE: a real crash (SystemExit/KeyboardInterrupt — BaseException) is
            # NOT caught here, so it propagates and the key stays IN-FLIGHT → a
            # later duplicate resolves to needs_human (no resend).
            except AmbiguousSendError as exc:
                if key:
                    self._sent.set(key, _NEEDS_HUMAN)
                log.warning("email send AMBIGUOUS — needs_human, not retried")
                return finish("needs_human", decision="allow", error=str(exc),
                              detail="ambiguous transport failure after a send attempt; "
                                     "NOT auto-retried — a human must verify delivery")
            except Exception as exc:  # noqa: BLE001 — a CLEAN failure (definitely not sent)
                if key:
                    self._sent.delete(key)  # safe: nothing was sent
                log.warning("email send failed (clean, not sent): %s", exc)
                return finish("failed", decision="allow", error=str(exc),
                              detail="send failed (not delivered)")

            # Clean success → mark SENT (at-most-once) and report.
            if key:
                self._sent.set(key, _SENT)
            log.info("email sent to %s via %s", recipient.split("@")[-1] if "@" in recipient else "?", endpoint)
            return finish("sent", decision="allow", detail="sent via SMTP+TLS")
        except Exception as exc:  # noqa: BLE001 — a channel reports, never crashes
            # (A true crash — SystemExit/KeyboardInterrupt — is BaseException, so it
            # is NOT caught here: it propagates, leaving the key in_flight.)
            try:
                self._send_audit.record(
                    proposal_id=context.get("proposal_id"), approval_id=approval_id,
                    channel=self.name, recipient=recipient, endpoint=endpoint,
                    content_hash=content_hash, decision="error", status="failed",
                )
            except Exception:  # noqa: BLE001
                pass
            return self._result("failed", recipient=recipient, endpoint=endpoint,
                                content_hash=content_hash, detail="unexpected error", error=str(exc))


# ── Registration into the action channel registry (E5) ──────────────────────


def register_email_channel(registry, *, enabled: bool | None = None, **channel_kwargs):
    """Register an :class:`EmailSendChannel` onto ``registry`` — but **only when
    explicitly enabled** (``MNESIS_EMAIL_ENABLED``, default OFF).

    Disabled (the default), ``email`` is not a known channel, so an email proposal
    fails closed at the gate (unknown channel). Even when enabled, the channel is
    **dry-run by default** and behind the **E1 egress plane** — registering it is
    not the same as permitting a send. Returns the (possibly unchanged) registry."""
    enabled = config.MNESIS_EMAIL_ENABLED if enabled is None else enabled
    if enabled:
        registry.register(EmailSendChannel(**channel_kwargs))
    return registry


def action_channel_registry(*, email_enabled: bool | None = None, **channel_kwargs):
    """The action agent's channel registry: the bundled **inert** channels plus the
    **email** channel *iff* enabled (E5). The default (email off) is byte-identical
    to :func:`channels.default_channel_registry` — email is opt-in."""
    from .channels import default_channel_registry

    return register_email_channel(
        default_channel_registry(), enabled=email_enabled, **channel_kwargs
    )
