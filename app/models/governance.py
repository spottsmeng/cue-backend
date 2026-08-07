import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, Enum, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base, Timestamped, TZDateTime, UUIDPk

# Governance-domain models (PRD §6.14, FR-ADM-07/08) live here, not in
# app/models/ledger.py or app/identity/models.py — same reasoning
# app/twin/models.py's own top-of-file note gives for the Twin domain: these
# are a distinct concern (consent, retention) from Ledger core or RBAC, even
# though both reference projects/parties/organisations.

# Fixed, structural state machine (PRD §4.1: PARTY ||--o{ CONSENT_RECORD;
# FR-CAP-06's "acceptance, objection and opt-out") — not domain vocabulary a
# tenant would extend, so a native enum, same reasoning
# app/models/ledger.py's CommitmentState comment gives for staying off
# ontology_terms.
ConsentStatus = Enum("pending", "accepted", "objected", "opted_out", name="consent_status")


class ConsentRecord(Base, UUIDPk, Timestamped):
    """FR-CAP-06/07, FR-ADM-07: the ledger a consent notice and its outcome
    are recorded into — one row per (party, project). Deliberately not the
    delivery mechanism itself: posting the actual bilingual notice to a
    vendor needs a real channel adapter, out of scope this session (see
    Prompt 5's EXPLICITLY OUT OF SCOPE). `evidence` is a plain text field
    naming what grounds the current `status` (a message excerpt once real
    capture exists, or the administering user's own action today) — not
    routed through the shared polymorphic `evidence` table the way
    Commitment/Budget are, since that table's CHECK constraint is exactly
    one of {commitment, budget} and there is no captured message to point at
    for most transitions this session can actually produce.
    """

    __tablename__ = "consent_records"
    __table_args__ = (
        UniqueConstraint("party_id", "project_id", name="consent_records_party_project_key"),
    )

    party_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("parties.id"), index=True
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), index=True, comment="RLS scope"
    )
    notice_sent_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    status: Mapped[str] = mapped_column(ConsentStatus, default="pending")
    evidence: Mapped[str | None] = mapped_column(default=None)


class RetentionPolicy(Base, UUIDPk, Timestamped):
    """FR-ADM-08: retention window configuration, per PRD §10.3's org x
    "project type" x region axes — deliberately minimal (no per-record-class
    dimension, even though §10.3's own table gives messages/ledger/documents/
    audit/consent each a different default) since nothing consumes this value
    yet: deletion automation is explicitly out of scope this session (Prompt
    5), so this table is configuration, not a resolver. `vertical_id` is the
    "project type" tier — CUE's own vocabulary for that axis elsewhere in
    this schema (app/models/vertical.py) — and `region` a free-text code
    (e.g. 'SG', 'CN'); either NULL broadens the policy to every value on that
    axis.
    """

    __tablename__ = "retention_policies"
    __table_args__ = (
        CheckConstraint("retention_days > 0", name="retention_policy_positive_window"),
    )

    organisation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organisations.id"), index=True, comment="RLS scope"
    )
    vertical_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("verticals.id"),
        default=None,
        index=True,
        comment="'project type' tier; NULL applies org-wide across every vertical",
    )
    region: Mapped[str | None] = mapped_column(default=None, comment="NULL applies to every region")
    retention_days: Mapped[int] = mapped_column()
