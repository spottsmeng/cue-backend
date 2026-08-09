import uuid
from datetime import datetime, time as dt_time

from sqlalchemy import CheckConstraint, Enum, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base, Timestamped, TZDateTime, UUIDPk
from app.identity.models import MembershipRole

# Foresight-domain models (PRD §6.9 FR-FOR, §6.7 FR-DEV, §6.15 FR-NTF) live
# here, not in app/models/ledger.py — that file's own top-of-file comment
# names Risk and Deviation among the entities "not modelled in this pass —
# belong to later build phases," same reasoning app/twin/models.py and
# app/documents/models.py already give for owning their domain's models in
# their own package rather than the shared app/models/ tree. Prompt 7's own
# instruction is explicit about the path: `app/foresight/models.py`, not
# `app/models/foresight.py`.
#
# Notification is one shared table for commitment/risk/deviation events
# (item 8) rather than three separate notification tables — the recipient-
# facing shape (collapsed_count, deliverable_at, downstream_consequence) is
# identical regardless of which domain object triggered it; only the FK
# differs, and all three are nullable with a CHECK that at least one is set
# (not "exactly one" the way Evidence's subject columns are, since a
# risk-triggered notification legitimately also names the commitment it
# concerns).

# "model_drift" (M10, NFR-OBS-05/FR-VOI-06) added via a later migration
# (see alembic/versions — the "M10 drift detection" revision) — a genuinely
# new source, not a reuse of app/documents/drift.py's own, unrelated
# "drift" concept (a circulated file differing from the project's approved
# DocumentVersion by hash, which reuses source="contradiction").
RiskSource = Enum("silence", "contradiction", "forecast", "model_drift", name="risk_source")
RiskSeverity = Enum("low", "medium", "high", "critical", name="risk_severity")
RiskStatus = Enum("open", "acknowledged", "resolved", "superseded", name="risk_status")

DeviationStatus = Enum("auto_drafted", "confirmed", "resolved", name="deviation_status")

NotificationChannel = Enum("webhook", "push", "email", "teams", name="notification_channel")

ForesightThresholdMetric = Enum(
    "silence_multiplier", "escalation_hours", "forecast_slack_days",
    name="foresight_threshold_metric",
)

ForesightAuditAction = Enum(
    "risk_created", "risk_superseded", "risk_acknowledged", "risk_escalated", "risk_resolved",
    "deviation_drafted", "deviation_confirmed", "deviation_resolved",
    "notification_created", "notification_delivered",
    "commitment_auto_transitioned",
    name="foresight_audit_action",
)


class Risk(Base, UUIDPk, Timestamped):
    """PRD §6.9 (FR-FOR) — a single surfaced finding from Silence Radar,
    contradiction/spec-drift detection or forecasting. `downstream_consequence`
    is NOT NULL (FR-FOR-09: "Never emit a bare alert... every surfaced item
    carries its computed downstream consequence") — the same enforced-column
    discipline CLAUDE.md requires for Commitment.evidence_span, applied here
    to the requirement's own explicit wording rather than a UI convention.
    If a detector cannot compute a downstream consequence for a candidate
    finding, that is a sign the finding isn't ready to exist yet, not a
    reason to make this column nullable.

    `finding_key` is FR-FOR-10's dedup identity — stable per underlying
    condition (e.g. `f"silence:{commitment_id}"`,
    `f"forecast:{milestone_id}"`, `f"contradiction:{claim_a_id}:{claim_b_id}"`,
    the pair sorted so either ordering hashes the same finding). All three
    creation paths (silence.py, contradiction.py, forecast.py) go through
    app/foresight/risk.py's single shared helper to check for an existing
    open Risk with the same (project_id, source, finding_key) before
    inserting — see that module for why this lives in one place, not three.

    `base_rate` (FR-FOR-02) is nullable and left unset, not fabricated, when
    there isn't yet a large enough historical sample to compute one — same
    "don't fabricate what you don't have" posture FR-TWN-08's own build note
    and CLAUDE.md's Models table both establish elsewhere in this project.
    """

    __tablename__ = "risks"
    # No DB-level uniqueness on (project_id, source, finding_key) — a
    # resolved finding can legitimately recur later as a brand-new open Risk
    # (a vendor goes quiet, recovers, then goes quiet again), so the same
    # finding_key can validly repeat across rows over time. FR-FOR-10's
    # dedup is "no two *open* findings for the same condition", which is an
    # application-level invariant (app/foresight/risk.py's shared helper
    # queries for an existing status='open' row before inserting), not a
    # column-level constraint a fixed key tuple could express correctly.

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), index=True, comment="RLS scope"
    )
    source: Mapped[str] = mapped_column(RiskSource)
    finding_key: Mapped[str] = mapped_column(
        comment="stable dedup identity for this underlying condition, see class docstring"
    )
    severity: Mapped[str] = mapped_column(RiskSeverity)
    status: Mapped[str] = mapped_column(RiskStatus, default="open")

    commitment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("commitments.id"), default=None, index=True
    )
    milestone_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("milestones.id"), default=None, index=True
    )
    spec_claim_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("spec_claims.id"), default=None, index=True
    )

    downstream_consequence: Mapped[str] = mapped_column(
        comment="FR-FOR-09: NOT NULL by design, see class docstring"
    )
    base_rate: Mapped[float | None] = mapped_column(
        default=None, comment="FR-FOR-02: null means 'no base rate yet', never fabricated"
    )
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)

    acknowledged_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), default=None
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    resolved_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)

    escalated_to_role: Mapped[str | None] = mapped_column(
        MembershipRole, default=None, comment="FR-FOR-08: last tier this Risk was escalated to"
    )
    escalated_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)

    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("risks.id"), default=None,
        comment="FR-FOR-10: set when a newer Risk replaces this one for the same finding_key",
    )


class Deviation(Base, UUIDPk, Timestamped):
    """PRD §6.7 (FR-DEV) — scope changes, delays and corrective actions.
    Evidence is NOT NULL, same shared-`evidence`-table + deferred-trigger
    mechanism as Commitment/Budget/DocumentVersion/SpecClaim (FR-DEV-02).

    `status` mirrors Commitment.verification_state's two-step shape
    (FR-DEV-04: "PM confirms or edits") rather than reusing that column's
    literal name, since "auto_drafted" reads more plainly here than
    "pending_verification" for a non-Ledger reader of this table.
    """

    __tablename__ = "deviations"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), index=True, comment="RLS scope"
    )
    class_term_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ontology_terms.id"),
        comment="ontology_terms row, category='deviation_class'",
    )
    status: Mapped[str] = mapped_column(DeviationStatus, default="auto_drafted")
    description_en: Mapped[str] = mapped_column()

    risk_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("risks.id"), default=None, index=True,
        comment="FR-DEV-04: the Risk whose detection auto-drafted this row, if any",
    )
    commitment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("commitments.id"), default=None, index=True
    )
    milestone_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("milestones.id"), default=None, index=True
    )

    resolution_date: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    resolution_owner: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), default=None
    )

    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), default=None
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)


class Notification(Base, UUIDPk):
    """PRD §6.15 (FR-NTF) — one row per recipient per (collapsed group of)
    finding. `downstream_consequence` is NOT NULL, same FR-FOR-09-style
    enforced-column discipline as Risk (FR-NTF-02: "Every notification
    carries the downstream consequence, never a bare event").

    `collapsed_risk_ids` is FR-NTF-03's collapsing, applied at creation time
    (app/foresight/notification.py's create_notification) — other open Risk
    ids folded into this one notification rather than each firing its own.
    This is a *different* dedup than Risk's own FR-FOR-10 check (item 4a):
    that one prevents duplicate Risk rows for the same underlying condition,
    before a Notification is ever created; this one merges *distinct,
    already-deduplicated* Risks that are related enough (same commitment or
    milestone, raised close together) to surface as one notification.

    `deliverable_at` is FR-NTF-04's quiet-hours eligibility instant, computed
    once at creation (app/foresight/notification.py) — this session computes
    *when* a notification becomes eligible to send; real push/email/Teams
    delivery is deferred (Prompt 11/13), so `delivered_via`/`sent_at` stay
    NULL except for the one real adapter this session does build (webhook).
    """

    __tablename__ = "notifications"
    __table_args__ = (
        CheckConstraint(
            "(risk_id IS NOT NULL) OR (deviation_id IS NOT NULL) OR (commitment_id IS NOT NULL)",
            name="notification_has_a_subject",
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), index=True, comment="RLS scope"
    )
    recipient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), index=True
    )
    risk_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("risks.id"), default=None, index=True
    )
    deviation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("deviations.id"), default=None, index=True
    )
    commitment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("commitments.id"), default=None, index=True
    )
    collapsed_risk_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), default=list)
    collapsed_count: Mapped[int] = mapped_column(default=1)

    severity: Mapped[str] = mapped_column(RiskSeverity)
    downstream_consequence: Mapped[str] = mapped_column(comment="FR-NTF-02: NOT NULL, see class docstring")

    deliverable_at: Mapped[datetime] = mapped_column(
        TZDateTime, comment="FR-NTF-04: earliest instant quiet hours allow this to be sent"
    )
    delivered_via: Mapped[str | None] = mapped_column(NotificationChannel, default=None)
    sent_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)

    acknowledged_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), default=None
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(
        TZDateTime, default=None, comment="FR-NTF-05: escalation checks this"
    )
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)


class WebhookSubscription(Base, UUIDPk, Timestamped):
    """Item 8's "/webhooks subscription endpoint" — a project registers a
    URL to receive commitment/risk/deviation events. `secret` is an
    HMAC-SHA256 signing key (app/foresight/notification.py's deliver_webhook)
    so a receiver can verify a delivery genuinely came from CUE, same
    "verified, not trusted" posture as everything else this codebase signs
    or checks. The only real delivery adapter this session builds — see
    Notification's own docstring for why webhook is not deferred the way
    push/email/Teams are (EXPLICITLY OUT OF SCOPE section of Prompt 7).
    """

    __tablename__ = "webhook_subscriptions"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), index=True, comment="RLS scope"
    )
    url: Mapped[str] = mapped_column()
    event_types: Mapped[list[str]] = mapped_column(
        ARRAY(String), comment="subset of {'commitment', 'risk', 'deviation'}"
    )
    secret: Mapped[str] = mapped_column(comment="HMAC-SHA256 signing key, generated at creation")
    active: Mapped[bool] = mapped_column(default=True)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))


class ForesightThreshold(Base, UUIDPk, Timestamped):
    """FR-FOR-07: per-project, per-deviation-class threshold configuration —
    extends app/models/governance.py's RetentionPolicy "axis, axis, value;
    NULL broadens" config-table pattern (org x vertical x region there;
    org x project x deviation_class here) rather than inventing a parallel
    mechanism, per Prompt 7's own explicit instruction.

    `metric` names *which* threshold this row configures (Silence Radar's
    multiplier over a vendor's baseline gap, escalation's unacknowledged-
    hours window, forecast's slack-days cutoff) — a fixed, closed set the
    application code branches on by name (app/foresight/threshold.py), same
    "structural, not domain vocabulary" reasoning app/models/ledger.py's
    CommitmentState comment gives for staying a native enum rather than an
    ontology_terms row. `project_id` NULL applies org-wide;
    `deviation_class_term_id` NULL applies to every class — resolution is
    app/foresight/threshold.py's resolve_threshold, most-specific-match over
    both axes, same ranking idiom app/twin/service.py's _most_specific /
    app/documents/service.py's _resolve_retention_policy already establish.
    """

    __tablename__ = "foresight_thresholds"
    __table_args__ = (
        UniqueConstraint(
            "organisation_id", "project_id", "deviation_class_term_id", "metric",
            name="foresight_thresholds_unique_scope",
            postgresql_nulls_not_distinct=True,
        ),
    )

    organisation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organisations.id"), index=True, comment="RLS scope"
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), default=None, index=True,
        comment="NULL applies org-wide across every project",
    )
    deviation_class_term_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ontology_terms.id"), default=None,
        comment="NULL applies to every deviation class",
    )
    metric: Mapped[str] = mapped_column(ForesightThresholdMetric)
    value: Mapped[float] = mapped_column()


class QuietHoursConfig(Base, UUIDPk, Timestamped):
    """FR-NTF-04: one row per project (decided over per-user — a project's
    quiet hours are an operational-rhythm property of the event/site, the
    same team-wide concern Project.timezone already is, not an individual
    preference; app/foresight/notification.py's docstring on
    compute_deliverable_at documents this choice). The "live-event window"
    override reuses Project.event_start/event_end rather than duplicating
    them here — that is already this project's own declared live-event
    window; no reason for a second copy.
    """

    __tablename__ = "quiet_hours_configs"
    __table_args__ = (UniqueConstraint("project_id", name="quiet_hours_configs_one_per_project"),)

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), index=True, comment="RLS scope"
    )
    quiet_start_local: Mapped[dt_time] = mapped_column(comment="local to Project.timezone")
    quiet_end_local: Mapped[dt_time] = mapped_column(comment="local to Project.timezone")
    critical_severity_threshold: Mapped[str] = mapped_column(
        RiskSeverity,
        default="critical",
        comment="FR-NTF-04: severity at/above this bypasses quiet hours during the live-event window",
    )


class ForesightAuditLog(Base, UUIDPk):
    """This domain's own append-only trail (same "a second narrow table
    costs nothing that a wider polymorphic redesign would risk" reasoning
    app/twin/models.py's TwinAuditLog docstring gives, and
    app/models/audit.py's AuditLog repeats) — separate from AuditLog
    (commitment-scoped) and DocumentAuditLog. Built with the SET-NULL-on-
    delete append-only carve-out from the start (e771d0318751's lesson,
    already applied up front by document_audit_log — same here)."""

    __tablename__ = "foresight_audit_log"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), index=True, comment="RLS scope"
    )
    risk_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("risks.id", ondelete="SET NULL"), default=None, index=True
    )
    deviation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("deviations.id", ondelete="SET NULL"), default=None, index=True
    )
    notification_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("notifications.id", ondelete="SET NULL"), default=None, index=True
    )
    action: Mapped[str] = mapped_column(ForesightAuditAction)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), default=None
    )
    occurred_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
