import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, Enum, ForeignKey, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base, Timestamped, TZDateTime, UUIDPk

# Write-back domain models (PRD §6.12 FR-WBK) — same per-domain-module-owns-
# its-models precedent app/twin/, app/documents/, app/foresight/, app/capture/
# and app/parties/ already established.
#
# FR-WBK-05 ("never post autonomously") is why draft/authorise/send are three
# separate, ordered fields on one row rather than three rows or a boolean
# flag — `authorised_by`/`authorised_at` are only ever set by a distinct
# authorise call, and `send` (app/writeback/service.py) refuses to run at all
# unless both are already non-NULL. Mirrors the deliberate separation
# FR-LED-08's three-tap verification already established (view/correct/
# confirm as genuinely separate steps), per Prompt 12's own instruction not
# to invent a new pattern.

OutboundMessageStatus = Enum("draft", "authorised", "sent", name="outbound_message_status")
OutboundMessageReplyOutcome = Enum(
    "transitioned", "escalated", name="outbound_message_reply_outcome"
)


class OutboundMessage(Base, UUIDPk, Timestamped):
    """One row per FR-WBK draft/send cycle, always tied to the specific
    commitment decision it confirms (PRD §5.2: "PM confirms decision ->
    write-back") — `commitment_id` is NOT NULL, both because that is what
    this feature actually does (confirm one decision at a time, never a
    free-floating message) and because app/ledger/audit.py's
    record_audit_event, which every send is logged through per FR-WBK-08,
    itself requires a non-NULL commitment_id (app/models/audit.py's
    AuditLog docstring).

    `channel_id` is "the originating vendor group" (FR-WBK-01) — resolved at
    draft time from the commitment's own evidence (the real captured message
    that established it), never re-derived at send time, so a later capture
    event can't silently redirect an already-drafted message to a different
    group. `to_external_id`/`language` are likewise frozen at draft time
    (app/writeback/compose.py), for the same reason: what a human authorised
    is exactly what gets sent, not a value re-resolved out from under them.

    `rate_limit_bucket` is FR-WBK-04's explicit bucket key (project's
    Prompt 12 spec names it as its own field, not something to derive
    implicitly from `sent_at` at query time) — `f"{channel_id}:{local_date}"`
    in the project's own timezone, set once, atomically, inside the same
    transaction as `sent_at` (app/writeback/rate_limit.py). Only a `sent`
    row ever gets one; a draft or authorised-but-unsent row does not count
    against the ceiling.
    """

    __tablename__ = "outbound_messages"
    __table_args__ = (
        CheckConstraint(
            "(status = 'draft' AND authorised_by IS NULL AND authorised_at IS NULL AND sent_at IS NULL) "
            "OR (status = 'authorised' AND authorised_by IS NOT NULL AND authorised_at IS NOT NULL "
            "AND sent_at IS NULL) "
            "OR (status = 'sent' AND authorised_by IS NOT NULL AND authorised_at IS NOT NULL "
            "AND sent_at IS NOT NULL)",
            name="outbound_message_status_field_consistency",
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), index=True, comment="RLS scope"
    )
    channel_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("channels.id"), index=True,
        comment="FR-WBK-01: the originating vendor group, resolved from the commitment's own evidence",
    )
    commitment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("commitments.id"), index=True,
        comment="the decision this message confirms — always set, see class docstring",
    )
    party_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("parties.id"),
        comment="the vendor being addressed — commitment.party_id, denormalised at draft time",
    )
    to_external_id: Mapped[str] = mapped_column(
        comment="ChannelIdentity.external_id resolved at draft time, frozen thereafter"
    )
    language: Mapped[str] = mapped_column(comment="bcp47 — FR-WBK-02's resolved target language")
    draft_text: Mapped[str] = mapped_column(
        comment="FR-WBK-03: a single closed question, in `language`"
    )
    status: Mapped[str] = mapped_column(OutboundMessageStatus, default="draft")

    authorised_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), default=None
    )
    authorised_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    sent_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    rate_limit_bucket: Mapped[str | None] = mapped_column(default=None, index=True)

    reply_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id"), default=None,
        comment="FR-WBK-06: the inbound Message matched as this outbound's reply",
    )
    reply_outcome: Mapped[str | None] = mapped_column(OutboundMessageReplyOutcome, default=None)
    reply_detail: Mapped[dict] = mapped_column(JSONB, default=dict)


WritebackAuditAction = Enum("config_updated", name="writeback_audit_action")


class WritebackAuditLog(Base, UUIDPk):
    """This domain's own append-only trail for events that are not a
    commitment mutation (so cannot go through app/ledger/audit.py's
    AuditLog, whose commitment_id is NOT NULL) — same "a second narrow
    table costs nothing a wider polymorphic redesign would risk" reasoning
    app/foresight/models.py's ForesightAuditLog docstring gives. Every send
    itself is logged via the shared AuditLog instead, per Prompt 12's own
    explicit instruction (a new AuditAction value, not a parallel log) —
    this table exists only for the rate ceiling's own "explicit, audited
    configuration change" requirement (FR-WBK-04), which has no commitment
    to hang off of.
    """

    __tablename__ = "writeback_audit_log"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), index=True, comment="RLS scope"
    )
    action: Mapped[str] = mapped_column(WritebackAuditAction)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), default=None
    )
    occurred_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
