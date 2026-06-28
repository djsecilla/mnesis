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
from typing import TYPE_CHECKING

from . import config
from .config import now_iso as _now
from .audit import AgentAuditLog
from .channels import RISK_EXTERNAL, ChannelRegistry, DeliveryResult, OutboundArtifact
from .egress import EgressPolicy, Recipient
from .proposals import ActionProposal, ActionProposalStore

if TYPE_CHECKING:
    pass

#: Artifact-metadata keys that would let *content* dictate where something is sent.
#: Their presence is a destination-integrity violation (anti-exfiltration).
_FORBIDDEN_ARTIFACT_KEYS = frozenset(
    {"destination", "to", "recipient", "recipients", "cc", "bcc", "send_to", "sendto", "address"}
)

#: Sources a recipient confirmation may legitimately come from (a human, not content).
_CONFIRM_SOURCES = frozenset({"policy", "user"})


class DestinationIntegrityError(Exception):
    """The destination was sourced from artifact/Mnesis content, not user/policy."""


class GateError(Exception):
    """A gate operation could not proceed (unknown/already-decided proposal)."""


class RecipientConfirmationError(Exception):
    """An external send was approved without a valid, matching recipient confirmation."""


class RecipientValidationError(Exception):
    """An EXTERNAL proposal's recipient failed E1 at **proposal time** — it is not
    policy/user-sourced + allowlisted, so no sendable proposal is ever formed."""


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
        egress: EgressPolicy | None = None,
    ) -> None:
        self._channels = channels
        self._store = store if store is not None else ActionProposalStore()
        self._audit = audit if audit is not None else AgentAuditLog()
        self._policy = policy if policy is not None else default_policy()
        #: Used to (re)validate recipients for EXTERNAL proposals (E1: allowlist +
        #: source=policy). External channels still re-run their own egress at send.
        self._egress = egress if egress is not None else EgressPolicy.from_config()

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
        # E1 at PROPOSAL time for an EXTERNAL channel: the recipient (attached by
        # the agent from policy/user structured input) must be policy/user-sourced
        # AND on the egress allowlist, or no sendable proposal is ever formed. A
        # content-sourced or non-allowlisted recipient is refused here, before the
        # proposal exists. (The send still re-runs the full E1 gate at transmit.)
        if risk == RISK_EXTERNAL:
            decision = self._egress.validate_recipient(Recipient(destination or "", "policy"))
            if decision.denied:
                raise RecipientValidationError(
                    f"external proposal refused at proposal time: {decision.reason} "
                    "— the recipient must be policy/user-sourced and on the egress "
                    "allowlist (it is never taken from content)"
                )

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
            # EXTERNAL can NEVER reach here — reaffirm A2's always-gated rule.
            assert risk != RISK_EXTERNAL, "external proposals can never be auto-approved"
            self._execute(proposal, edited=False, auto=True)
            return self._store.get(proposal.id)  # the updated (executed) proposal

        self._audit.write_action_event("proposed", proposal)
        return proposal  # PAUSED — execution requires explicit approval

    # -- presentation (what the human reviews before approving) ----------------

    def present(self, proposal_id: str) -> dict:
        """The review presentation for a proposal. For an **external** proposal it
        shows prominently — recipient, channel, egress endpoint, a **dry-run
        rendered preview** of the exact message, the rationale + citations, and that
        an explicit **recipient confirmation** is required to approve."""
        p = self._store.get(proposal_id)
        if p is None:
            raise GateError(f"no action proposal with id {proposal_id!r}")
        meta = (p.artifact or {}).get("metadata") or {}
        view: dict = {
            "id": p.id, "action_type": p.action_type, "channel": p.channel,
            "risk_class": p.risk_class, "status": p.status,
            "recipient": p.destination, "rationale": p.rationale,
            "citations": list(meta.get("citations") or []),
        }
        if p.risk_class == RISK_EXTERNAL:
            artifact = OutboundArtifact(**p.artifact)
            preview = self._channels.get(p.channel).preview(artifact, p.destination)
            allowlisted = self._egress.validate_recipient(
                Recipient(p.destination or "", "policy")).allowed
            view.update({
                "endpoint": preview.endpoint,
                "recipient_allowlisted": allowlisted,
                "recipient_confirmation_required": True,
                "dry_run_preview": {
                    "subject": preview.subject,
                    "body": preview.body,                    # for the approver's eyes
                    "recipient": preview.recipient,
                    "endpoint": preview.endpoint,
                    "content_hash": preview.content_hash,
                    "secret_findings": preview.secret_findings,
                },
            })
        return view

    # -- approve / edit / reject -----------------------------------------------

    def approve(
        self,
        proposal_id: str,
        *,
        confirm_recipient: "str | Recipient | None" = None,
        edited_artifact: OutboundArtifact | dict | None = None,
        edited_destination: str | None = None,
        note: str | None = None,
    ) -> DeliveryResult:
        """Approve a pending proposal and execute it **exactly once** via its channel.

        For an **EXTERNAL** proposal, approving content is NOT approving a recipient:
        ``confirm_recipient`` is **required** and must (a) be policy/user-sourced and
        (b) exactly match the proposal's (possibly edited) recipient, which must
        itself pass E1 (allowlist + source=policy). A content-only approval (no
        ``confirm_recipient``) does **not** send. Editing the recipient re-runs E1;
        editing content re-renders the preview + re-runs the payload scan (in the
        channel, at send)."""
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

        artifact = OutboundArtifact(**artifact_dict)
        self._validate_destination(destination, artifact)  # artifact never sets the recipient

        recipient_confirmed = False
        if proposal.risk_class == RISK_EXTERNAL:
            recipient_confirmed = self._confirm_recipient(destination, confirm_recipient)

        proposal.artifact = artifact_dict
        proposal.destination = destination
        proposal.edited = edited
        proposal.recipient_confirmed = recipient_confirmed
        if note:
            proposal.decision_note = note
        return self._execute(proposal, edited=edited, auto=False)

    def _confirm_recipient(self, destination: str | None, confirm_recipient) -> bool:
        """Enforce explicit recipient confirmation for an external send. Returns True
        on success; raises :class:`RecipientConfirmationError` otherwise."""
        conf_addr, conf_src = self._resolve_confirm(confirm_recipient)
        if conf_addr is None:
            raise RecipientConfirmationError(
                "external send requires an explicit recipient confirmation "
                "(approve(..., confirm_recipient=<the exact recipient>)) — approving "
                "content is not approving a recipient"
            )
        if conf_src not in _CONFIRM_SOURCES:
            raise RecipientConfirmationError(
                f"recipient confirmation must be policy/user-sourced (got source={conf_src!r}); "
                "a content/model/artifact-sourced recipient is never accepted"
            )
        if conf_addr.strip().lower() != (destination or "").strip().lower():
            raise RecipientConfirmationError(
                "confirm_recipient does not match the proposal's recipient"
            )
        # The (possibly edited) recipient must pass E1: allowlisted + policy-sourced.
        decision = self._egress.validate_recipient(Recipient(destination or "", "policy"))
        if decision.denied:
            raise RecipientConfirmationError(f"recipient refused by egress policy: {decision.reason}")
        return True

    @staticmethod
    def _resolve_confirm(confirm) -> tuple[str | None, str | None]:
        if confirm is None:
            return None, None
        if isinstance(confirm, Recipient):
            return confirm.address.strip(), (confirm.source or "").strip().lower()
        return str(confirm).strip(), "user"  # a bare string is a human's confirmation

    def reject(self, proposal_id: str, reason: str = "") -> ActionProposal:
        """Reject a pending proposal — discard it, deliver nothing. Audited."""
        proposal = self._require_pending(proposal_id)
        updated = self._store.update(
            proposal_id, status="rejected", decision_note=reason or "rejected",
        )
        self._audit.write_action_event("rejected", updated)
        return updated

    def expire(self, proposal_id: str, reason: str = "expired") -> ActionProposal:
        """Expire a pending proposal (e.g. it sat too long) — deliver nothing. A
        distinct terminal status from ``rejected``; like it, it cannot then run."""
        proposal = self._require_pending(proposal_id)
        updated = self._store.update(proposal_id, status="expired", decision_note=reason)
        self._audit.write_action_event("expired", updated)
        return updated

    # -- the single execution path (the only place a channel is invoked) -------

    def _execute(self, proposal: ActionProposal, *, edited: bool, auto: bool) -> DeliveryResult:
        artifact = OutboundArtifact(**proposal.artifact)
        # Re-validate integrity on the *executed* artifact/destination (an edit
        # could have introduced a content-sourced destination).
        self._validate_destination(proposal.destination, artifact)

        approval_id = uuid.uuid4().hex[:16]   # one approval → one execution
        result = self._channels.deliver(
            proposal.channel, artifact, proposal.destination,
            context={
                "proposal_id": proposal.id,           # at-most-once idempotency key
                "approval_id": approval_id,           # which approval triggered the send
                "action_type": proposal.action_type,
                # The recipient was human-confirmed at the gate → policy-sourced, so
                # the channel's own egress (E1) check accepts the source.
                "recipient_source": "policy",
            },
        )
        # Reflect the channel's outcome on the proposal: a delivered/sent send is
        # "executed"; a dry-run/blocked/needs_human/failed result keeps that status
        # (all terminal — the proposal ran exactly once at the gate).
        status = "executed" if result.ok else result.status
        updated = self._store.update(
            proposal.id, status=status, artifact=proposal.artifact,
            destination=proposal.destination, edited=edited, result=asdict(result),
            decision_note=proposal.decision_note, recipient_confirmed=proposal.recipient_confirmed,
        )
        if result.ok:
            event = "auto_executed" if auto else "executed"
        else:
            event = "execute_failed" if result.status == "failed" else f"execute_{result.status}"
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
