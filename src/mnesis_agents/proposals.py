"""The dream-cycle **proposals queue** — a generic, human-review surface.

The dream cycle never auto-applies a knowledge-changing op; it records each as a
**proposal** here, where a human (the Web UI G11 review screen, or a later
surface) approves and applies it through the existing review machinery. Nothing
in this module resolves a contradiction or merges a page — it only persists
suggestions.

Two proposal kinds today:
  - ``contradiction`` — which page to keep in an open Mnesis contradiction review.
    It carries the Mnesis ``review_id`` so it **annotates** that existing review
    (a recommendation attached by id), without ever calling ``mnesis_resolve``.
  - ``duplicate`` — a proposed merge/supersession of a near-duplicate page pair.

Storage is a single append-with-upsert JSONL under the agents' artefact dir
(gitignored). Each proposal has a **stable id** derived from its identifying
fields, so re-proposing the same thing on the next cycle updates one entry
instead of piling duplicates — repeated cycles are idempotent.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_id(kind: str, key_fields: dict[str, Any]) -> str:
    blob = json.dumps({"kind": kind, **key_fields}, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


@dataclass
class Proposal:
    """One queued, human-reviewable proposal (never auto-applied)."""

    id: str
    kind: str                       # "contradiction" | "duplicate"
    status: str                     # "open" | "accepted" | "dismissed"
    created: str
    updated: str
    detail: dict[str, Any] = field(default_factory=dict)
    cycle_started: str | None = None


class ProposalStore:
    """Append-with-upsert JSONL queue of dream-cycle proposals."""

    def __init__(self, directory: Path | str | None = None, *, filename: str = "proposals.jsonl") -> None:
        self.directory = Path(directory or config.MNESIS_AGENTS_PROPOSALS_DIR)
        self._path = self.directory / filename

    # -- read ------------------------------------------------------------------

    def _load(self) -> dict[str, Proposal]:
        """Latest record per id wins (rewrite compaction keeps it small anyway)."""
        out: dict[str, Proposal] = {}
        if not self._path.is_file():
            return out
        with open(self._path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rec = json.loads(line)
                    out[rec["id"]] = Proposal(**rec)
        return out

    def all(self) -> list[Proposal]:
        return sorted(self._load().values(), key=lambda p: (p.created, p.id))

    def list_open(self) -> list[Proposal]:
        return [p for p in self.all() if p.status == "open"]

    def get(self, proposal_id: str) -> Proposal | None:
        return self._load().get(proposal_id)

    # -- write -----------------------------------------------------------------

    def _rewrite(self, items: dict[str, Proposal]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            for p in sorted(items.values(), key=lambda p: (p.created, p.id)):
                fh.write(json.dumps(asdict(p), ensure_ascii=False) + "\n")
        tmp.replace(self._path)

    def upsert(
        self,
        kind: str,
        key_fields: dict[str, Any],
        detail: dict[str, Any],
        *,
        cycle_started: str | None = None,
    ) -> Proposal:
        """Record (or refresh) a proposal. Idempotent on ``(kind, key_fields)``:
        re-proposing the same thing updates the one entry, never duplicates it.
        A proposal a human already actioned (accepted/dismissed) is left alone."""
        pid = _stable_id(kind, key_fields)
        items = self._load()
        existing = items.get(pid)
        if existing is not None and existing.status != "open":
            return existing  # don't resurrect a human-actioned proposal
        now = _now()
        proposal = Proposal(
            id=pid,
            kind=kind,
            status="open",
            created=existing.created if existing else now,
            updated=now,
            detail=detail,
            cycle_started=cycle_started,
        )
        items[pid] = proposal
        self._rewrite(items)
        return proposal

    def set_status(self, proposal_id: str, status: str) -> Proposal | None:
        """Mark a proposal accepted/dismissed (a human decision; not used by the
        cycle itself). Returns the updated proposal or None if unknown."""
        items = self._load()
        p = items.get(proposal_id)
        if p is None:
            return None
        p.status = status
        p.updated = _now()
        self._rewrite(items)
        return p


# ── Action proposals (A2: the approval-gate surface) ────────────────────────


@dataclass
class ActionProposal:
    """A proposed outbound action awaiting human approval before any channel runs.

    Recorded durably so it survives a restart and can be listed/approved/rejected
    out-of-band (the CLI ``mnesis-agents actions``, and the Web review screen
    later). The ``artifact`` is the serialized :class:`channels.OutboundArtifact`;
    ``result`` is the serialized :class:`channels.DeliveryResult` once executed.
    """

    id: str
    action_type: str
    channel: str
    risk_class: str                                 # inert | external (from the channel)
    artifact: dict[str, Any]
    destination: str | None
    rationale: str
    status: str                                     # pending | executed | rejected | failed
    created: str
    updated: str
    result: dict[str, Any] | None = None
    decision_note: str | None = None
    edited: bool = False

    def summary(self) -> str:
        title = (self.artifact or {}).get("title", "")
        return (f"{self.id}  [{self.status}]  {self.action_type} via {self.channel} "
                f"({self.risk_class}) -> {self.destination}  \"{title}\"")


class ActionProposalStore:
    """Durable JSONL queue of action proposals (an extension of the M4 proposals
    store, same mechanics + directory). Unlike the idempotent dream-cycle queue,
    each composed action is a **distinct** proposal (a caller-supplied unique id);
    the gate inserts one and later updates its status/result in place."""

    def __init__(self, directory: Path | str | None = None, *, filename: str = "action_proposals.jsonl") -> None:
        from .triggers.connector import path_lock

        self.directory = Path(directory or config.MNESIS_AGENTS_PROPOSALS_DIR)
        self._path = self.directory / filename
        self._lock = path_lock(self._path)

    def _load(self) -> dict[str, ActionProposal]:
        out: dict[str, ActionProposal] = {}
        if not self._path.is_file():
            return out
        with open(self._path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rec = json.loads(line)
                    out[rec["id"]] = ActionProposal(**rec)
        return out

    def _rewrite(self, items: dict[str, ActionProposal]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            for p in sorted(items.values(), key=lambda p: (p.created, p.id)):
                fh.write(json.dumps(asdict(p), ensure_ascii=False) + "\n")
        tmp.replace(self._path)

    def all(self) -> list[ActionProposal]:
        return sorted(self._load().values(), key=lambda p: (p.created, p.id))

    def list_pending(self) -> list[ActionProposal]:
        return [p for p in self.all() if p.status == "pending"]

    def get(self, proposal_id: str) -> ActionProposal | None:
        return self._load().get(proposal_id)

    def put(self, proposal: ActionProposal) -> ActionProposal:
        with self._lock:
            items = self._load()
            items[proposal.id] = proposal
            self._rewrite(items)
        return proposal

    def update(self, proposal_id: str, **changes: Any) -> ActionProposal | None:
        with self._lock:
            items = self._load()
            p = items.get(proposal_id)
            if p is None:
                return None
            for k, v in changes.items():
                setattr(p, k, v)
            p.updated = _now()
            items[proposal_id] = p
            self._rewrite(items)
        return p
