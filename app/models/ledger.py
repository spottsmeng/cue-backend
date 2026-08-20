import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, Enum, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base, Timestamped, TZDateTime, UUIDPk

# Fixed, structural state machines (PRD §6.5, §4.3) — not domain vocabulary that
# grows over time, so these stay native enums rather than ontology_terms rows.
# Contrast with act_type/class_term_id/type_term_id below, which *are*
# ontology-governed (CUE-Tech-Stack.md §5).
CommitmentState = Enum(
    "proposed", "committed", "at_risk", "delivered", "broken", "renegotiated", "withdrawn",
    name="commitment_state",
)
VerificationState = Enum(
    "auto", "pending_verification", "human_verified", "human_corrected",
    name="verification_state",
)
PaymentStatus = Enum("unpaid", "invoiced", "paid", name="payment_status")

# Scope note: Meeting, Risk and Deviation (PRD §4.1) are not modelled in this
# pass — they belong to later build phases. Dependency lives in
# app/twin/models.py; Document/DocumentVersion/SpecClaim live in
# app/documents/models.py (both own-module precedents this file's Evidence
# class straddles, since it's referenced by all of them). This file covers
# the Ledger core: Commitment, Evidence, Deliverable, Milestone, Budget.


class Milestone(Base, UUIDPk, Timestamped):
    """PRD §4.1: MILESTONE ||--o{ DEPENDENCY; DELIVERABLE }o--|| MILESTONE : belongs_to"""

    __tablename__ = "milestones"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), index=True
    )
    type_term_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ontology_terms.id"),
        comment="ontology_terms row, category='milestone_type'",
    )
    name: Mapped[str] = mapped_column()
    planned_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    actual_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    is_fixed: Mapped[bool] = mapped_column(
        default=False, comment="FR-TWN-11: immovable node (doors open, venue hand-back)"
    )


class Deliverable(Base, UUIDPk, Timestamped):
    """PRD §4.1: COMMITMENT }o--o| DELIVERABLE : concerns"""

    __tablename__ = "deliverables"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), index=True
    )
    class_term_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ontology_terms.id"),
        comment="ontology_terms row, category='deliverable_class'",
    )
    milestone_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("milestones.id"), default=None, index=True
    )
    name: Mapped[str] = mapped_column()


class Commitment(Base, UUIDPk, Timestamped):
    """PRD §4.3 — the core object.

    The evidence-linkage requirement (FR-LED-03 / CLAUDE.md: "No commitment
    without evidence... verified in code, not trusted") is not expressible as a
    column-level constraint, since it depends on a *child table* having at least
    one row. It is enforced by a deferred constraint trigger added in the Alembic
    migration (`check_commitment_has_evidence`), not visible on this class — do
    not read the absence of a constraint here as the absence of enforcement.
    """

    __tablename__ = "commitments"
    __table_args__ = (
        CheckConstraint(
            "currency IS NULL OR length(currency) = 3", name="commitment_currency_iso4217_length"
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), index=True
    )
    party_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("parties.id"), comment="who owes it"
    )
    counterparty_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("parties.id"), comment="whom it is owed to"
    )
    deliverable_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("deliverables.id"), default=None
    )
    act_type_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ontology_terms.id"),
        comment="ontology_terms row, category='commitment_act' (universal core, PRD §4.2.1)",
    )
    state: Mapped[str] = mapped_column(CommitmentState, default="proposed")

    # Field names match the enforced extraction contract exactly (CLAUDE.md's
    # extraction rules, cue-eval/schema.json) — not PRD §4.3's `description` /
    # `description_original` wording. A noun phrase naming the thing promised
    # ("LED screen install"), never a restatement of the message, never a time
    # or price — CLAUDE.md: "The model buries them there otherwise."
    deliverable_en: Mapped[str] = mapped_column(comment="canonical EN noun phrase")
    deliverable_original: Mapped[str | None] = mapped_column(
        default=None,
        comment="verbatim source language; equals deliverable_en if source is EN",
    )
    due_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    amount: Mapped[float | None] = mapped_column(Numeric(12, 2), default=None)
    currency: Mapped[str | None] = mapped_column(default=None)
    payment_status: Mapped[str | None] = mapped_column(
        PaymentStatus,
        default=None,
        comment="FR-LED-13: Finance-role-set only, never inferred by the model",
    )

    confidence: Mapped[float] = mapped_column(default=0.0)
    field_confidence: Mapped[dict] = mapped_column(JSONB, default=dict)
    verification_state: Mapped[str] = mapped_column(VerificationState, default="auto")
    verification_reasons: Mapped[list[str]] = mapped_column(
        ARRAY(String),
        default=list,
        comment=(
            "Why this row is in the review queue, set at extraction time. "
            "verification_state says a human must look; this says what to look "
            "at. Every route into pending_verification collapses into one "
            "state on purpose (one queue a PM checks, not five), which leaves "
            "a triage problem: a price to confirm and a possible hallucination "
            "are the same colour without this."
        ),
    )
    verified_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), default=None
    )
    verified_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)

    supersedes: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), default=list)


CommitmentSupersessionCandidateStatus = Enum(
    "pending", "confirmed", "rejected", name="commitment_supersession_candidate_status"
)


class CommitmentSupersessionCandidate(Base, UUIDPk, Timestamped):
    """FR-LED-05: an AI-*proposed* candidate link — "commitment does this
    appear to revise commitment that" — never applied to `Commitment.
    supersedes` until a human confirms it (app/ledger/supersession.py owns
    the propose/confirm/reject lifecycle). Same auto-drafted-then-human-
    confirmed shape app/foresight/models.py's `Deviation` already
    established (`status`: auto_drafted -> confirmed) and app/writeback/
    models.py's `OutboundMessage` established a third time (draft ->
    authorised -> sent) — reused here rather than reinvented, because
    CLAUDE.md's "no commitment without evidence... verified in code, not
    trusted" extends naturally to "no supersession link without a human
    confirming it, verified in code": the model proposes a relationship
    between two *existing*, already-evidenced commitments, a human decides
    whether it's real, and only a `confirmed` row ever mutates
    `Commitment.supersedes`.

    `commitment_id` is the newer commitment (the one that may be a
    revision); `supersedes_commitment_id` is the older one it may revise.
    Only ever written by app/ledger/supersession.py's `propose_
    supersession_candidates` when the model's own judgment is "yes, this
    looks like a revision" — no row is written for a "no" verdict, same
    "only surface a real finding" posture app/foresight/risk.py's own
    dedup-on-write discipline establishes for detector output elsewhere in
    this codebase.

    No `organisation_id` column, same direct-`project_id`-only shape
    `Deviation`/`Risk`/`AuditLog` already use (RLS via a join through
    `projects`, not a denormalised org column) — this is a Ledger-scoped
    concept about two commitments in one project, not an org-wide one the
    way `VendorMetric` genuinely is.
    """

    __tablename__ = "commitment_supersession_candidates"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), index=True, comment="RLS scope"
    )
    commitment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("commitments.id"), index=True,
        comment="the newer commitment — the one that may be a revision",
    )
    supersedes_commitment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("commitments.id"), index=True,
        comment="the older commitment it may revise",
    )
    reasoning: Mapped[str] = mapped_column(comment="the model's own stated reasoning, verbatim")
    status: Mapped[str] = mapped_column(CommitmentSupersessionCandidateStatus, default="pending")
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), default=None
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)


class Evidence(Base, UUIDPk):
    """PRD §4.3. Belongs to exactly one of {commitment, budget, document
    version, spec claim, deviation} — enforced by the CHECK constraint below,
    and is what the deferred trigger on each owning table checks for
    existence of.

    `document_version_id` / `spec_claim_id` were added in the Documents
    session (Prompt 6) — extending this same shared table rather than giving
    either domain its own, since both need exactly the shape already here
    (channel/sent_at/language/original_text/span for a captured message, or
    the "manual" channel for FR-LED-10-style caller-supplied provenance).
    See app/documents/models.py's own module docstring for why.
    `deviation_id` was added the same way in the Foresight session
    (Prompt 7) — see app/foresight/models.py's Deviation docstring.
    """

    __tablename__ = "evidence"
    __table_args__ = (
        CheckConstraint(
            "(commitment_id IS NOT NULL)::int + (budget_id IS NOT NULL)::int "
            "+ (document_version_id IS NOT NULL)::int + (spec_claim_id IS NOT NULL)::int "
            "+ (deviation_id IS NOT NULL)::int = 1",
            name="evidence_exactly_one_subject",
        ),
    )

    commitment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("commitments.id"), default=None, index=True
    )
    budget_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("budgets.id"), default=None, index=True
    )
    document_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document_versions.id"), default=None, index=True
    )
    spec_claim_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("spec_claims.id"), default=None, index=True
    )
    # Added in the Foresight session (Prompt 7) — FR-DEV-02's "attach
    # supporting evidence ... to each deviation" is the same
    # "verified-in-code-not-trusted" evidence discipline as every other
    # subject on this table, so it extends the shared table rather than
    # Deviation inventing its own (app/foresight/models.py's Deviation
    # docstring).
    deviation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("deviations.id"), default=None, index=True
    )

    message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("messages.id"),
        default=None,
        index=True,
        comment="app/capture/models.py's Message — real FK since Prompt 11 item 1; NULL for evidence "
        "with no captured-message source (a manual entry, or a document/spec-claim's own evidence)",
    )
    channel: Mapped[str] = mapped_column(
        comment="provenance tag — channel_types.code by convention, but not FK-enforced: "
        "a display/audit label, not an adapter-selection key"
    )
    sent_at: Mapped[datetime] = mapped_column(TZDateTime)
    language: Mapped[str] = mapped_column(comment="bcp47")
    original_text: Mapped[str] = mapped_column(comment="never overwritten")
    translation: Mapped[str | None] = mapped_column(default=None)
    media_ref: Mapped[str | None] = mapped_column(default=None, comment="signed, expiring URI")
    transcript_confidence: Mapped[float | None] = mapped_column(default=None, comment="voice only")
    span_start: Mapped[int | None] = mapped_column(default=None)
    span_end: Mapped[int | None] = mapped_column(default=None)


class Budget(Base, UUIDPk):
    """PRD §4.3. `is_current` marks the active baseline; superseded baselines are
    kept via `revision_of` rather than deleted, per FR-ADM-11's revision-history
    requirement."""

    __tablename__ = "budgets"
    __table_args__ = (
        CheckConstraint("length(currency) = 3", name="budget_currency_iso4217_length"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), index=True
    )
    approved_amount: Mapped[float] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column()
    approved_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), comment="Finance/Producer role only"
    )
    approved_at: Mapped[datetime] = mapped_column(TZDateTime)
    revision_of: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("budgets.id"), default=None
    )
    is_current: Mapped[bool] = mapped_column(default=True)
