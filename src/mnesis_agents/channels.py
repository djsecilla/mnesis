"""The OutboundChannel pattern — THE contract every delivery mechanism implements.

This is the **outbound mirror** of the writing agents' `SourceConnector`: where a
connector turns the *world* into normalized inbound events, a channel turns an
agent's produced *artifact* into an outbound delivery. Future delivery mechanisms
(email, Slack, calendar, a webhook) implement this same interface, so the action
agent and its gate treat them uniformly.

A channel does **one job**: deliver. It does NOT decide whether it is *allowed* to
run — that is the gate's job (A2). The load-bearing piece of the contract is the
**`risk_class`**, which every channel declares:

  - **`inert`** — nothing reaches a third party: the effect is local/operator-
    scoped (a draft file on disk, a local notification). Safe to run.
  - **`external`** — the effect leaves the box / reaches a third party (a sent
    email, a posted message). The gate treats every `external` channel as
    **always-gated** (proposed / human-approved), never auto-fired.

The default ``risk_class`` is **``external``** — the conservative side — so a
channel that forgets to declare one is treated as risky and gated, never as safe.

**This module ships only INERT channels** — `DraftOutboxChannel` and
`LocalNotifyChannel`. There is deliberately **no external-send channel here**; the
interface merely makes the risk explicit so A2's gate can be written against it.
"""
from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config

log = logging.getLogger("mnesis_agents.channels")

#: Risk classes (see the module docstring). ``external`` is the gated default.
RISK_INERT = "inert"
RISK_EXTERNAL = "external"
RISK_CLASSES = frozenset({RISK_INERT, RISK_EXTERNAL})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:60] or "artifact"


# ── Artifact + result ───────────────────────────────────────────────────────


@dataclass
class OutboundArtifact:
    """What an action agent wants to deliver — content, not a command to send."""

    kind: str                                   # e.g. "brief", "notification", "digest"
    title: str
    body: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class DeliveryResult:
    """The outcome of a single delivery — what happened and where it landed.

    Inert channels use ``status`` ``delivered``/``failed``; an external send
    channel adds ``dry_run``/``sent``/``blocked``/``needs_human`` and records the
    ``recipient``, ``endpoint``, and ``content_hash`` — but **never the body or any
    secret**.
    """

    channel: str
    risk_class: str
    status: str                                 # delivered | failed | dry_run | sent | blocked | needs_human
    destination: str | None = None              # the (local/operator-scoped) target
    location: str | None = None                 # where it landed (path / "console")
    detail: str = ""
    error: str | None = None
    # External-send fields (None for inert channels).
    recipient: str | None = None
    endpoint: str | None = None
    content_hash: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("delivered", "sent")


@dataclass
class ChannelPreview:
    """A **dry-run preview** of what a channel WOULD deliver — for the human at the
    gate to review before approving an external send. Renders nothing externally.

    Shows the body for the *approver's eyes* (the body is never logged/audited);
    ``content_hash`` and ``secret_findings`` let the gate flag a payload-scan hit.
    """

    channel: str
    risk_class: str
    recipient: str | None
    endpoint: str | None
    subject: str
    body: str
    content_hash: str | None = None
    secret_findings: list[str] = field(default_factory=list)


# ── The interface ───────────────────────────────────────────────────────────


class OutboundChannel(ABC):
    """Base class for outbound delivery channels (see the module docstring).

    Subclasses set ``name`` + ``risk_class`` and implement :meth:`deliver`.
    """

    #: Channel name (how the registry / agent refers to it).
    name: str = "channel"
    #: Risk class — ``inert`` or ``external``. Defaults to the gated side.
    risk_class: str = RISK_EXTERNAL

    @abstractmethod
    def deliver(
        self, artifact: OutboundArtifact, destination: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> DeliveryResult:
        """Deliver ``artifact`` to ``destination`` (channel-specific). Returns a
        :class:`DeliveryResult`; must not raise for an ordinary failure — it
        reports ``status="failed"`` with a reason instead."""

    def endpoint(self) -> str | None:
        """The egress endpoint this channel would send to (external channels set it;
        inert channels have none)."""
        return None

    def preview(
        self, artifact: OutboundArtifact, destination: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> ChannelPreview:
        """A dry-run preview of what WOULD be delivered (sends nothing). External
        channels override to render the exact message + run the payload scan."""
        return ChannelPreview(
            self.name, self.risk_class, recipient=destination, endpoint=self.endpoint(),
            subject=artifact.title, body=artifact.body,
        )

    # Small helpers for subclasses to build consistent results.
    def _ok(self, *, destination, location, detail="") -> DeliveryResult:
        return DeliveryResult(self.name, self.risk_class, "delivered",
                              destination=destination, location=location, detail=detail)

    def _failed(self, *, destination, error) -> DeliveryResult:
        return DeliveryResult(self.name, self.risk_class, "failed",
                              destination=destination, error=str(error))


# ── Inert channel: draft outbox ─────────────────────────────────────────────


class DraftOutboxChannel(OutboundChannel):
    """Write the artifact as a **draft file** in the outbox — never sends anywhere.

    The draft is a Markdown document with a YAML metadata header (channel, risk,
    kind, destination, timestamp, and the artifact's own metadata) followed by the
    body. A human reviews/sends it later; this channel only drafts.
    """

    name = "draft-outbox"
    risk_class = RISK_INERT

    def __init__(self, outbox: Path | str | None = None) -> None:
        self.outbox = Path(outbox or config.MNESIS_ACTION_OUTBOX)

    def deliver(
        self, artifact: OutboundArtifact, destination: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> DeliveryResult:
        try:
            self.outbox.mkdir(parents=True, exist_ok=True)
            path = self.outbox / f"{_stamp()}-{_slug(artifact.title or artifact.kind)}.md"
            header = {
                "channel": self.name,
                "risk_class": self.risk_class,
                "status": "draft",
                "kind": artifact.kind,
                "title": artifact.title,
                "destination": destination,
                "created": _now(),
                "metadata": dict(artifact.metadata or {}),
                "context": dict(context or {}),
            }
            text = _yaml_frontmatter(header) + (artifact.body or "").strip() + "\n"
            path.write_text(text, encoding="utf-8")
            log.info("draft written to outbox: %s", path)
            return self._ok(destination=destination, location=str(path),
                            detail="draft written to outbox (not sent)")
        except Exception as exc:  # noqa: BLE001 — a channel reports failure, never crashes
            return self._failed(destination=destination, error=exc)


# ── Inert channel: local notify ─────────────────────────────────────────────


class LocalNotifyChannel(OutboundChannel):
    """Notify ONLY the local operator — logs to the console and appends a line to a
    local notifications file (JSONL). No third-party recipient is ever involved."""

    name = "local-notify"
    risk_class = RISK_INERT

    def __init__(self, notify_file: Path | str | None = None) -> None:
        self.notify_file = Path(notify_file or config.MNESIS_ACTION_NOTIFY_FILE)

    def deliver(
        self, artifact: OutboundArtifact, destination: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> DeliveryResult:
        target = destination or "operator"
        try:
            record = {
                "ts": _now(),
                "channel": self.name,
                "risk_class": self.risk_class,
                "destination": target,
                "kind": artifact.kind,
                "title": artifact.title,
                "message": artifact.body,
                "context": dict(context or {}),
            }
            log.info("local notification [%s] %s: %s", target, artifact.kind, artifact.title)
            self.notify_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.notify_file, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            return self._ok(destination=target, location=str(self.notify_file),
                            detail="notified local operator (console + notifications file)")
        except Exception as exc:  # noqa: BLE001
            return self._failed(destination=target, error=exc)


# ── Registry ────────────────────────────────────────────────────────────────


class ChannelRegistry:
    """Maps channel names → instances; the action agent delivers through it."""

    def __init__(self, channels: list[OutboundChannel] | None = None) -> None:
        self._by_name: dict[str, OutboundChannel] = {}
        for ch in channels or []:
            self.register(ch)

    def register(self, channel: OutboundChannel) -> None:
        self._by_name[channel.name] = channel

    def get(self, name: str) -> OutboundChannel:
        if name not in self._by_name:
            raise KeyError(f"no channel named {name!r}; have {sorted(self._by_name)}")
        return self._by_name[name]

    def names(self) -> list[str]:
        return sorted(self._by_name)

    def risk_class(self, name: str) -> str:
        return self.get(name).risk_class

    def __contains__(self, name: str) -> bool:
        return name in self._by_name

    def deliver(
        self, name: str, artifact: OutboundArtifact, destination: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> DeliveryResult:
        return self.get(name).deliver(artifact, destination, context)


def default_channel_registry() -> ChannelRegistry:
    """The default registry of the bundled **inert** channels (no external send)."""
    return ChannelRegistry([DraftOutboxChannel(), LocalNotifyChannel()])


# ── Helpers ─────────────────────────────────────────────────────────────────


def _yaml_frontmatter(header: dict) -> str:
    import yaml

    return "---\n" + yaml.safe_dump(header, sort_keys=False, allow_unicode=True).strip() + "\n---\n\n"
