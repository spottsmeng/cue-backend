import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, model_validator

from app.reports.schema import CommitmentSummary, DecisionLogRow, ReportField, ReportProvenance, RiskLogRow, VendorStatusRow

# The Ask assistant's typed contract (PRD §6.11 FR-ASK, §12.5). Reuses
# app/reports/schema.py's row/field types directly wherever the same
# underlying data is being shaped for a different consumer (CommitmentSummary,
# DecisionLogRow, RiskLogRow, VendorStatusRow, ReportField, ReportProvenance)
# rather than a second, parallel set of types — Prompt 9's own instruction
# for the successor brief, applied the same way to summarise's reuse of the
# report composer's own row shapes.


# --- query (FR-ASK-01/02/03/08) ------------------------------------------

CitationSourceTypeLiteral = Literal[
    "commitment", "evidence", "budget", "document_version", "audit_log", "deviation"
]


class Citation(BaseModel):
    """§12.5: 'every citation opens the source' — `source_id` always
    resolves to a real row in the table `source_type` names, never a
    synthetic id. For a citation grounded in a captured message (Evidence),
    resolved to whichever of the five subjects that Evidence row actually
    belongs to (app/ask/answer.py's _resolve_citation) — a vendor-chat
    citation about a commitment reads as "commitment", not the more generic
    "evidence", since that is the more useful thing for a PM to open."""

    source_type: CitationSourceTypeLiteral
    source_id: uuid.UUID
    label: str | None = None
    snippet: str | None = None


class AskQueryRequest(BaseModel):
    question: str
    conversation_id: uuid.UUID | None = None


# Why a request wasn't answered — a caller-checkable discriminator, not a
# string the model might or might not phrase consistently. "no_citable_source"
# is FR-ASK-02's own refusal (retrieval found nothing, or nothing retrieved
# supported a confident answer); "action_not_yet_supported" is FR-ASK-06's
# own — the request was recognised as asking CUE to take an outbound action
# (app/ask/intent.py's classify_intent), which Write-back (not yet built)
# would be needed for. Set entirely by code (app/ask/answer.py), never by
# the model's own free-text output.
AskRefusalKindLiteral = Literal["no_citable_source", "action_not_yet_supported"]


class AskAnswerOut(BaseModel):
    """FR-ASK-02: 'an answer with no citable source must say so rather than
    assert.' FR-ASK-06: 'do not fake an action taken response — say it
    can't do that yet.' The ReportField idiom (app/reports/schema.py)
    applied to both: `available=False` plus a typed `refusal_kind` is what a
    caller must structurally branch on, not a string the model might or
    might not produce. The invariants below are enforced by Pydantic itself,
    not just convention — CLAUDE.md/§8.4's "enforce, don't ask" discipline,
    applied to this response shape exactly the way it's applied to the
    extraction schema: it is impossible to construct an `available=True`
    answer with zero citations or a `refusal_kind` set, or an
    `available=False` answer that carries prose or omits a `refusal_kind`.
    """

    available: bool
    answer: str | None
    unavailable_reason: str | None = None
    refusal_kind: AskRefusalKindLiteral | None = None
    citations: list[Citation] = []
    conversation_id: uuid.UUID

    @model_validator(mode="after")
    def _no_unsupported_claim(self) -> "AskAnswerOut":
        if self.available:
            if not self.citations:
                raise ValueError("an available=True answer must carry at least one citation (FR-ASK-02)")
            if self.refusal_kind is not None:
                raise ValueError("an available=True answer must not carry a refusal_kind")
        else:
            if self.answer is not None:
                raise ValueError("available=False must not carry prose (FR-ASK-02/FR-ASK-06)")
            if self.refusal_kind is None:
                raise ValueError("available=False must carry a refusal_kind (FR-ASK-02/FR-ASK-06)")
        return self


# --- summarise (FR-ASK-04/05) ---------------------------------------------

AskSummaryVariantLiteral = Literal[
    "project_status", "vendor_status", "period_digest", "decision_history", "outstanding_actions"
]


class AskSummaryRequest(BaseModel):
    variant: AskSummaryVariantLiteral
    # period_digest only.
    period_start: datetime | None = None
    period_end: datetime | None = None


class ProjectStatusSummary(BaseModel):
    project_name: ReportField
    open_commitment_count: ReportField
    at_risk_commitment_count: ReportField
    open_risk_count: ReportField
    open_deviation_count: ReportField
    next_milestone_name: ReportField
    next_milestone_planned_at: ReportField


class AskVendorStatusSummary(BaseModel):
    vendors: list[VendorStatusRow]
    reliability_data_available: bool


class PeriodDigestSummary(BaseModel):
    period_start: datetime
    period_end: datetime
    commitments_created: list[CommitmentSummary]
    commitments_resolved: list[CommitmentSummary]
    decisions: list[DecisionLogRow]


class DecisionHistorySummary(BaseModel):
    decisions: list[DecisionLogRow]


DueWindowLiteral = Literal[
    "overdue", "due_within_7_days", "due_within_30_days", "due_later", "no_due_date"
]


class OutstandingActionsByOwner(BaseModel):
    party_id: uuid.UUID
    party_name: str
    commitments: list[CommitmentSummary]


class OutstandingActionsByWindow(BaseModel):
    window: DueWindowLiteral
    commitments: list[CommitmentSummary]


class OutstandingActionsSummary(BaseModel):
    """FR-ASK-05 — distinct from GET /commitments's raw state/party/due-
    window *filters* (a caller picks one axis at a time there): this is the
    same open-commitment set grouped both ways at once, assistant-shaped
    rather than a filterable table."""

    by_owner: list[OutstandingActionsByOwner]
    by_due_window: list[OutstandingActionsByWindow]


class AskSummaryOut(BaseModel):
    """Exactly one of the five payload fields is populated, matching
    `variant` — a discriminated-by-request-field response, same "the caller
    already told us which one" shape a REST resource with several read
    variants naturally has."""

    variant: AskSummaryVariantLiteral
    project_status: ProjectStatusSummary | None = None
    vendor_status: AskVendorStatusSummary | None = None
    period_digest: PeriodDigestSummary | None = None
    decision_history: DecisionHistorySummary | None = None
    outstanding_actions: OutstandingActionsSummary | None = None


# --- successor-brief (FR-ASK-07) ------------------------------------------


class KeyDocumentRow(BaseModel):
    document_id: uuid.UUID
    name: str
    current_version_id: uuid.UUID | None
    approved: bool
    provenance: ReportProvenance


class VendorContactRow(BaseModel):
    party_id: uuid.UUID
    display_name: str
    open_commitment_count: int


class DeviationResolutionRow(BaseModel):
    deviation_id: uuid.UUID
    status: str
    description_en: str
    resolution_date: datetime | None
    resolution_owner: uuid.UUID | None
    provenance: ReportProvenance


class SuccessorBriefOut(BaseModel):
    """FR-ASK-07: 'a full handover context pack for an incoming PM in one
    action' — §12.5's own list, verbatim: open commitments, decision
    history, risks, key documents, vendor contacts, deviations and
    resolutions."""

    project_id: uuid.UUID
    generated_at: datetime
    open_commitments: list[CommitmentSummary]
    decision_history: list[DecisionLogRow]
    risks: list[RiskLogRow]
    key_documents: list[KeyDocumentRow]
    vendor_contacts: list[VendorContactRow]
    deviations_and_resolutions: list[DeviationResolutionRow]
