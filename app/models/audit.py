import uuid
from datetime import datetime

from sqlalchemy import Enum, ForeignKey, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base, TZDateTime, UUIDPk
from app.models.ledger import CommitmentState

# Fixed set of ledger-mutation kinds this audit trail records — a mechanism
# concept (like CommitmentState), not domain vocabulary, so it stays a native
# enum rather than an ontology_terms row. FR-LED-09 (corrections as training
# signal) is why "corrected" is distinguished from "verified": a
# `verification_state` of human_corrected with an empty `detail.changes`
# would be a silent contradiction. "payment_status_updated" (migration
# 9ddb100d7e8e, same ADD VALUE pattern d7def2e27c7c's twin_audit_action
# additions already established) is FR-LED-13's own structurally distinct
# path — never folded into "corrected", since payment_status is Finance-set
# and must never be inferred from message content the way a correction is.
# "outbound_sent" (migration 9197d521030d, same ADD VALUE pattern) is
# FR-WBK-08's own real send, logged through this shared trail per Prompt
# 12's own instruction — a reply-driven transition still logs its own
# separate "state_transition" row; the send itself is a distinct event on
# the same commitment.
AuditAction = Enum(
    "created", "verified", "corrected", "state_transition", "payment_status_updated",
    "outbound_sent",
    name="audit_action",
)


class AuditLog(Base, UUIDPk):
    """FR-LED-12: an immutable, append-only log of every ledger mutation —
    who, what, when, from what evidence.

    Append-only is enforced in the DB (a BEFORE UPDATE OR DELETE trigger, see
    the migration), not just by omitting update/delete code paths here — the
    same "verified in code, not trusted" posture CLAUDE.md sets for evidence.

    Scoped to commitment mutations only for this build slice (Twin/Documents/
    Deviations audit is a later phase, per Prompt 2's explicit scope) —
    `commitment_id` is NOT NULL rather than a generalised polymorphic subject,
    since a generalised shape isn't needed yet and CLAUDE.md says not to
    design for hypothetical future requirements.
    """

    __tablename__ = "audit_log"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), index=True, comment="RLS scope"
    )
    commitment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("commitments.id"), index=True
    )
    action: Mapped[str] = mapped_column(AuditAction)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), default=None
    )
    occurred_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    from_state: Mapped[str | None] = mapped_column(CommitmentState, default=None)
    to_state: Mapped[str | None] = mapped_column(CommitmentState, default=None)
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("evidence.id"),
        default=None,
        comment="from what evidence, when the mutation has one",
    )
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
