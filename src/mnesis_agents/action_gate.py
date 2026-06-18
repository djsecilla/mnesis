"""The approval gate — the safety keystone for outbound actions (A2).

**No channel executes without a human approval.** When an action agent composes an
action, it goes through this gate, which:

  1. validates **destination integrity** (the destination comes from policy/user
     input — *never* from Mnesis content or the composed artifact);
  2. enforces the **always-gated rule** — every `external` channel is gated no
     matter what; `inert` channels are gated too (a future policy flag *could*
     auto-run an inert one, but it is OFF and external can never be auto-run);
  3. records an :class:`ActionProposal` and **pauses** — emitting the proposal for
     a human, executing nothing.

A human then **approves** (execute via the named channel), **edits** (execute the
edited artifact/destination), or **rejects** (discard) it through the approvals
surface (`mnesis-agents actions`, and the Web review screen later). Every outcome
— proposed, executed, failed, rejected — is **audited** (F6; identities and
destinations only, never the artifact body).

This is the durable, out-of-band counterpart of the F6 in-loop human-in-the-loop
interrupt: the gate is the **single, fail-closed path** to any side effect — a
channel is *only ever* invoked from inside :meth:`ActionGate._execute`.
"""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from . import config
from .audit import AgentAuditLog
from .channels import RISK_EXTERNAL, ChannelRegistry, DeliveryResult, OutboundArtifact
from .proposals import ActionProposal, ActionProposalStore

if TYPE_CHECKING:
    pass

#: Artifact-metadata keys that would let *content* dictate where something is sent.
#: Their presence is a destination-integrity violation (anti-exfiltration).
_FORBIDDEN_ARTIFACT_KEYS = frozenset(
    {"destination", "to", "recipient", "recipients", "cc", "bcc", "send_to", "sendto", "address"}
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DestinationIntegrityError(Exception):
    """The destination was sourced from artifact/Mnesis content, not user/policy."""


class GateError(Exception):
    """A gate operation could not proceed (unknown/already-decided proposal)."""


@dataclass
class ActionPolicy:
    """Action-gate policy. In this set **everything is gated**.

    ``auto_run_inert`` is the future escape hatch (OFF by default) that could let an
    INERT channel run without approval. EXTERNAL channels are **always gated** and
    this flag never applies to them.
    """

    auto_run_inert: bool = False


def default_policy() -> ActionPolicy:
    return ActionPolicy(auto_run_inert=config.MNESIS_ACTIONS_AUTO_RUN_INERT)


class ActionGate:
    """The non-bypassable approval gate over a :class:`ChannelRegistry`."""

    def __init__(
        self,
        channels: ChannelRegistry,
        *,
        store: ActionProposalStore | None = None,
        audit: AgentAuditLog | None = None,
        policy: ActionPolicy | None = None,
    ) -> None:
        self._channels = channels
        self._store = store if store is not None else ActionProposalStore()
        self._audit = audit if audit is not None else AgentAuditLog()
        self._policy = policy if policy is not None else default_policy()

    @property
    def store(self) -> ActionProposalStore:
        return self._store

    # -- the always-gated rule -------------------------------------------------

    def _must_gate(self, risk_class: str) -> bool:
        """True if this action must wait for approval. EXTERNAL is ALWAYS gated;
        INERT is gated unless the (off-by-default) auto-run flag is set."""
        if risk_class == RISK_EXTERNAL:
            return True  # never auto-run, regardless of policy
        return not self._policy.auto_run_inert

    # -- destination integrity -------------------------------------------------

    @staticmethod
    def _validate_destination(destination: str | None, artifact: OutboundArtifact) -> None:
        """The destination must come from policy/user input — never be sourced from
        the artifact/Mnesis content. An artifact that carries a destination-control
        field is refused (anti-exfiltration / prompt-injection)."""
        meta = artifact.metadata or {}
        smuggled = sorted(k for k in meta if k.lower() in _FORBIDDEN_ARTIFACT_KEYS)
        if smuggled:
            raise DestinationIntegrityError(
                f"artifact must not set the destination (found {smuggled}); the "
                "destination comes from policy/user input only"
            )

    # -- compose → pause (propose) ---------------------------------------------

    def propose(
        self,
        *,
        action_type: str,
        channel: str,
        artifact: OutboundArtifact,
        destination: str | None,
        rationale: str = "",
    ) -> ActionProposal:
        """Gate a composed action. Validates integrity + the always-gated rule and
        records a **pending** proposal (executing nothing) — the run pauses here.

        If (and only if) the channel is INERT *and* the auto-run flag is explicitly
        enabled, the action runs immediately (still audited). EXTERNAL channels can
        never reach that path."""
        ch = self._channels.get(channel)  # KeyError → unknown channel (fail closed)
        risk = ch.risk_class
        self._validate_destination(destination, artifact)

        proposal = ActionProposal(
            id=uuid.uuid4().hex[:16],
            action_type=action_type,
            channel=channel,
            risk_class=risk,
            artifact=asdict(artifact),
            destination=destination,
            rationale=rationale,
            status="pending",
            created=_now(),
            updated=_now(),
        )

        self._store.put(proposal)
        if not self._must_gate(risk):
            # Inert + operator-enabled auto-run: execute now (still the gate's path).
            self._execute(proposal, edited=False, auto=True)
            return self._store.get(proposal.id)  # the updated (executed) proposal

        self._audit.write_action_event("proposed", proposal)
        return proposal  # PAUSED — execution requires explicit approval

    # -- approve / edit / reject -----------------------------------------------

    def approve(
        self,
        proposal_id: str,
        *,
        edited_artifact: OutboundArtifact | dict | None = None,
        edited_destination: str | None = None,
        note: str | None = None,
    ) -> DeliveryResult:
        """Approve a pending proposal and execute it **exactly once** via its
        channel. ``edited_artifact``/``edited_destination`` (from the human) replace
        the proposed values; the edited destination is still integrity-checked."""
        proposal = self._require_pending(proposal_id)

        artifact_dict = dict(proposal.artifact)
        edited = False
        if edited_artifact is not None:
            new = edited_artifact if isinstance(edited_artifact, dict) else asdict(edited_artifact)
            artifact_dict.update(new)
            edited = True
        destination = proposal.destination
        if edited_destination is not None:
            destination = edited_destination
            edited = True

        proposal.artifact = artifact_dict
        proposal.destination = destination
        proposal.edited = edited
        if note:
            proposal.decision_note = note
        return self._execute(proposal, edited=edited, auto=False)

    def reject(self, proposal_id: str, reason: str = "") -> ActionProposal:
        """Reject a pending proposal — discard it, deliver nothing. Audited."""
        proposal = self._require_pending(proposal_id)
        updated = self._store.update(
            proposal_id, status="rejected", decision_note=reason or "rejected",
        )
        self._audit.write_action_event("rejected", updated)
        return updated

    # -- the single execution path (the only place a channel is invoked) -------

    def _execute(self, proposal: ActionProposal, *, edited: bool, auto: bool) -> DeliveryResult:
        artifact = OutboundArtifact(**proposal.artifact)
        # Re-validate integrity on the *executed* artifact/destination (an edit
        # could have introduced a content-sourced destination).
        self._validate_destination(proposal.destination, artifact)

        result = self._channels.deliver(
            proposal.channel, artifact, proposal.destination,
            context={"proposal_id": proposal.id, "action_type": proposal.action_type},
        )
        status = "executed" if result.ok else "failed"
        updated = self._store.update(
            proposal.id, status=status, artifact=proposal.artifact,
            destination=proposal.destination, edited=edited, result=asdict(result),
            decision_note=proposal.decision_note,
        )
        event = ("auto_executed" if auto else "executed") if result.ok else "execute_failed"
        self._audit.write_action_event(event, updated)
        return result

    def _require_pending(self, proposal_id: str) -> ActionProposal:
        proposal = self._store.get(proposal_id)
        if proposal is None:
            raise GateError(f"no action proposal with id {proposal_id!r}")
        if proposal.status != "pending":
            raise GateError(
                f"proposal {proposal_id!r} is {proposal.status!r}, not pending "
                "(already decided — a proposal executes at most once)"
            )
        return proposal
