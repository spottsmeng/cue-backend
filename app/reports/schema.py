import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

# The Living WIP report's typed contract (PRD §6.10 FR-RPT, §12.2, and the
# Challenge Brief's own Annex A sample report). §8.4's provenance-enforcement
# constraint ("the report composer refuses to render a field without a
# resolvable provenance link... a null/unavailable field with a reason, never
# a silently omitted field or a fabricated placeholder") is structural here,
# not a UI convention this session leaves to a future frontend to invent:
# every scalar figure in the report is a ReportField, whose `available` flag
# and `unavailable_reason` are how that refusal is expressed in the response
# body itself.

ReportFormatLiteral = Literal["pptx", "pdf"]
ReportTriggerLiteral = Literal["manual", "scheduled"]

# Mirrors app/api/schemas.py's VerificationStateLiteral, plus "not_applicable"
# for figures with no verification concept at all (a Milestone's planned_at,
# say — verification_state only ever means something for a Commitment field).
ReportVerificationStateLiteral = Literal[
    "auto", "pending_verification", "human_verified", "human_corrected", "not_applicable"
]


class ReportProvenance(BaseModel):
    """FR-RPT-04: "attach a provenance link to every figure and status."
    `source_type` names which table/row grounds this value; `source_id` is
    that row's id (None only for a value with no single row to point at,
    e.g. a project-level constant). `label` is a short human-readable note
    for a figure that's an aggregate over several rows (see ReportField's
    own docstring) — a tap on the future frontend's provenance chip resolves
    `source_id` back to its original message/evidence, same as
    CommitmentOut.evidence already supports."""

    source_type: str
    source_id: uuid.UUID | None
    label: str | None = None


class ReportField(BaseModel):
    """Generic wrapper for every scalar figure/status the report renders.
    `available=False` with `unavailable_reason` set is this composer's
    concrete implementation of PRD §8.4's refusal rule — the field is still
    present in the response (never silently dropped), its value is simply
    `None` and the reason is named, rather than a fabricated number.

    `provenance` is a list, not a single pointer, because several figures
    here (committed_spend, outstanding_payments) are aggregates over many
    Commitment rows — every row that contributed is named, not just one.

    `verification_state` mirrors the underlying source's own state where one
    applies (a budget-summary figure's Commitment.verification_state); it is
    `"not_applicable"` for a figure with no such concept (a Milestone's
    planned_at) rather than left null, so a frontend never has to special-
    case "verification_state is None" as an extra branch beyond the six
    literal values.
    """

    value: Any | None
    available: bool
    unavailable_reason: str | None = None
    verification_state: ReportVerificationStateLiteral = "not_applicable"
    provenance: list[ReportProvenance] = []


# --- Section 1: project overview (FR-RPT-02, FR-RPT-03) -----------------


class ProjectOverviewVisualReference(BaseModel):
    """FR-RPT-03: "embed images, renders and visual references, version-
    resolved to the current approved asset." Resolved via the only FK path
    Deliverable and Document actually share — SpecClaim.deliverable_id ->
    DocumentVersion -> Document (app/documents/models.py) — since Document
    itself carries no direct deliverable_id. `available=False` (current
    version not yet approved, or no document linked at all via a spec claim)
    is reported structurally, never a stale or unapproved image substituted
    in its place (FR-RPT-03: "always the current version, never a stale
    one")."""

    deliverable_id: uuid.UUID
    deliverable_name: str
    document_id: uuid.UUID | None
    document_version_id: uuid.UUID | None
    image_url: str | None
    available: bool
    unavailable_reason: str | None
    provenance: list[ReportProvenance]


class ProjectOverviewSection(BaseModel):
    project_name: ReportField
    client_name: ReportField
    venue: ReportField
    event_start: ReportField
    event_end: ReportField
    current_phase: ReportField
    visual_references: list[ProjectOverviewVisualReference]
    # Deliverables with no document linked via a SpecClaim yet — a count,
    # not a per-row entry for each one (PRD §12.2's "empty states: explain
    # what is missing" satisfied at the aggregate level; a per-deliverable
    # row for every one of what can be dozens of not-yet-documented
    # deliverables would swamp the section rather than inform it).
    deliverables_without_visual_reference: int


# --- Section 2: milestone tracker (Twin) ---------------------------------

MilestoneStatusLiteral = Literal["completed", "on_track", "at_risk", "critical", "no_date"]


class MilestoneTrackerRow(BaseModel):
    milestone_id: uuid.UUID
    name: str
    planned_at: datetime | None
    actual_at: datetime | None
    status: MilestoneStatusLiteral
    is_critical: bool
    slack_days: float | None
    provenance: ReportProvenance


# --- Section 3: vendor status summary (Commitments; VRG optional) -------


class CommitmentSummary(BaseModel):
    commitment_id: uuid.UUID
    deliverable_en: str
    state: str
    due_at: datetime | None
    amount: float | None
    currency: str | None
    verification_state: str
    provenance: ReportProvenance


class VendorStatusRow(BaseModel):
    """Grouped by Party (vendor org), not a finer "vendor category" — the
    `vendor_category` ontology_terms category exists in the schema's own
    comment (app/models/ontology.py) but no session has ever seeded or
    populated it, so grouping by it would be inventing structure that
    doesn't exist yet, not reading real data."""

    party_id: uuid.UUID
    party_name: str
    outstanding_actions: list[CommitmentSummary]
    confirmed_deliverables: list[CommitmentSummary]
    reliability: ReportField


class VendorStatusSection(BaseModel):
    vendors: list[VendorStatusRow]
    reliability_data_available: bool


# --- Section 4: budget summary (§6.10's exact four-field grounding) -----


class BudgetSummarySection(BaseModel):
    """§6.10's own paragraph, verbatim: approved budget from the Budget
    baseline; committed spend as the sum of amount across commitments in
    committed/at_risk/delivered state; outstanding payments as the sum of
    commitments where payment_status is not paid; variance as approved
    budget less committed spend. Computed nowhere else — see
    app/reports/composer.py's compute_budget_summary, the single
    implementation of this paragraph, reused by both the report and the
    export verification gate (FR-RPT-06)."""

    currency: str | None
    approved_budget: ReportField
    committed_spend: ReportField
    outstanding_payments: ReportField
    variance: ReportField


# --- Section 5: risk and issues log (Risk *and* Deviation, FR-DEV-05) ---


class RiskLogRow(BaseModel):
    risk_id: uuid.UUID
    source: str
    severity: str
    status: str
    downstream_consequence: str
    base_rate: float | None
    provenance: ReportProvenance


class DeviationLogRow(BaseModel):
    deviation_id: uuid.UUID
    class_code: str | None
    status: str
    description_en: str
    provenance: ReportProvenance


class RiskAndIssuesSection(BaseModel):
    """FR-DEV-05: deviations roll into this section alongside risks — both
    lists are populated from the same "still open" scoping (Risk:
    open/acknowledged; Deviation: auto_drafted/confirmed), consistent with
    FR-RPT-01's "continuously current" — a resolved/superseded finding is
    history, not a current issue, and belongs to the ledger's own audit
    trail rather than this section."""

    risks: list[RiskLogRow]
    deviations: list[DeviationLogRow]


# --- Section 6: decision and approval log (AuditLog) ---------------------


class DecisionLogRow(BaseModel):
    audit_log_id: uuid.UUID
    commitment_id: uuid.UUID
    action: str
    actor_id: uuid.UUID | None
    occurred_at: datetime
    from_state: str | None
    to_state: str | None
    provenance: ReportProvenance


class OutstandingApprovalRow(BaseModel):
    commitment_id: uuid.UUID
    deliverable_en: str
    due_at: datetime | None
    provenance: ReportProvenance


class DecisionAndApprovalLogSection(BaseModel):
    decisions: list[DecisionLogRow]
    outstanding_approvals: list[OutstandingApprovalRow]


# --- Section 7: next steps (open Commitments + Milestones) --------------


class NextStepMilestoneRow(BaseModel):
    milestone_id: uuid.UUID
    name: str
    planned_at: datetime | None
    provenance: ReportProvenance


class NextStepsSection(BaseModel):
    open_commitments: list[CommitmentSummary]
    open_milestones: list[NextStepMilestoneRow]


class LivingWipReportOut(BaseModel):
    """PRD §6.10/§12.2: the seven sections, in the Challenge Brief's own
    order (Annex A / p.3-4: project overview, milestone tracker, vendor
    status summary, budget summary, risk and issues log, decision and
    approval log, next steps). `generated_at` doubles as §12.2's "currency
    indicator... timestamp of last ledger change" proxy — this is always a
    fresh computation (no cache), so "when was this generated" and "as of
    when is this current" are the same instant."""

    project_id: uuid.UUID
    generated_at: datetime
    project_overview: ProjectOverviewSection
    milestone_tracker: list[MilestoneTrackerRow]
    vendor_status: VendorStatusSection
    budget_summary: BudgetSummarySection
    risk_and_issues: RiskAndIssuesSection
    decision_and_approval_log: DecisionAndApprovalLogSection
    next_steps: NextStepsSection


# --- Export / snapshot / schedule request-response shapes ---------------


class ExportBlockedCommitment(BaseModel):
    commitment_id: uuid.UUID
    deliverable_en: str
    reason: Literal["pending_verification"] = "pending_verification"


class ReportSnapshotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    format: ReportFormatLiteral
    template_code: str
    trigger: ReportTriggerLiteral
    generated_by: uuid.UUID | None
    generated_at: datetime
    download_url: str


class ReportSnapshotDetailOut(ReportSnapshotOut):
    report_json: dict


class ReportScheduleConfigOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    day_of_week: int
    hour_local: int
    minute_local: int
    format: ReportFormatLiteral
    template_code: str
    active: bool
    last_run_at: datetime | None
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime


class ReportScheduleConfigCreate(BaseModel):
    day_of_week: int
    hour_local: int
    minute_local: int = 0
    format: ReportFormatLiteral = "pptx"
    template_code: str = "cue_placeholder"


class ReportScheduleConfigUpdate(BaseModel):
    day_of_week: int | None = None
    hour_local: int | None = None
    minute_local: int | None = None
    format: ReportFormatLiteral | None = None
    template_code: str | None = None
    active: bool | None = None
