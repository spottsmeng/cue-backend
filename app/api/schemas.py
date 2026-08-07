import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Mirrors app/models/ledger.py's CommitmentState / VerificationState /
# PaymentStatus native enums — kept as Literals here (not imported from the
# SQLAlchemy Enum objects) since this is the API's own typed contract, same
# separation extraction already draws between cue-eval/schema.json and
# app/ledger/schema.py's ExtractedCommitment.
CommitmentStateLiteral = Literal[
    "proposed", "committed", "at_risk", "delivered", "broken", "renegotiated", "withdrawn"
]
VerificationStateLiteral = Literal["auto", "pending_verification", "human_verified", "human_corrected"]
PaymentStatusLiteral = Literal["unpaid", "invoiced", "paid"]

# Mirrors app/identity/models.py's MembershipRole native enum — same
# separation the ledger Literals above already draw from their SQLAlchemy
# Enum counterparts.
MembershipRoleLiteral = Literal[
    "project_manager", "producer", "finance", "account_manager", "designer",
    "administrator", "delegate", "read_only",
]


class MembershipCreate(BaseModel):
    """FR-ADM-06's 'assign members' step. Keyed by email, not user_id — an
    admin knows a colleague's email, not their internal uuid, and there is no
    SCIM pre-provisioning (out of scope this session) to invite someone who
    has never signed in; the user must already exist in this organisation
    (i.e. have authenticated at least once)."""

    email: str
    role: MembershipRoleLiteral


class MembershipOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    project_id: uuid.UUID
    role: MembershipRoleLiteral
    granted_at: datetime
    granted_by: uuid.UUID | None


class DelegationCreate(BaseModel):
    """FR-ADM-03: time-boxed delegation. `expires_at` is required — there is
    no un-time-boxed delegation, by design (PRD: 'time-boxed delegation for
    absence and handover, with scope and expiry')."""

    delegate_email: str
    role: MembershipRoleLiteral
    expires_at: datetime


class DelegationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    delegator_id: uuid.UUID
    delegate_id: uuid.UUID
    role: MembershipRoleLiteral
    granted_at: datetime
    granted_by: uuid.UUID
    expires_at: datetime
    revoked_at: datetime | None
    revoked_by: uuid.UUID | None


class ProjectCreate(BaseModel):
    name: str
    client_name: str | None = None
    venue: str | None = None
    timezone: str = "Asia/Singapore"
    vertical_code: str = "event-production"
    # FR-TWN-02: the Twin's archetype is seeded relative to this date (day 0
    # = the archetype's 'doors' node) — previously accepted by the Project
    # model but never actually settable through this schema, so every
    # project's event_start was silently always NULL. Optional: a project
    # can still be created before the event date is known, in which case the
    # archetype is materialized with its structure (FR-TWN-01) but every
    # planned_at left null until a PM sets it (FR-TWN-10, same as any other
    # override).
    event_start: datetime | None = None
    event_end: datetime | None = None
    # FR-TWN-09: the archetype to seed the Twin from — omit to get the most
    # specific `is_default` template for this org/vertical (see
    # app/twin/service.py's _resolve_archetype); pass a specific code to
    # pick a named template outright (e.g. a gala vs. a booth), the same
    # "pick a template or take the default" pattern most project-management
    # tools already use.
    archetype_code: str | None = None
    # FR-ADM-06: provisioning and initial member assignment as one call, under
    # the brief's 10-minute bar — the creator is granted "administrator"
    # membership automatically (see app/api/projects.py), this is for anyone
    # else who should have access from the start.
    members: list[MembershipCreate] = []


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organisation_id: uuid.UUID
    vertical_id: uuid.UUID
    name: str
    client_name: str | None
    venue: str | None
    timezone: str
    event_start: datetime | None
    event_end: datetime | None
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime


class EvidenceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    channel: str
    sent_at: datetime
    language: str
    original_text: str
    translation: str | None
    span_start: int | None
    span_end: int | None


class CommitmentOut(BaseModel):
    """§11.1: 'Every commitment field returned includes confidence,
    verification_state and evidence[]' — evidence is always populated here,
    never left for the client to fetch separately."""

    id: uuid.UUID
    project_id: uuid.UUID
    party_id: uuid.UUID
    counterparty_id: uuid.UUID
    deliverable_id: uuid.UUID | None
    act_type_id: uuid.UUID
    state: CommitmentStateLiteral
    deliverable_en: str
    deliverable_original: str | None
    due_at: datetime | None
    amount: float | None
    currency: str | None
    payment_status: PaymentStatusLiteral | None
    confidence: float
    field_confidence: dict
    verification_state: VerificationStateLiteral
    verified_by: uuid.UUID | None
    verified_at: datetime | None
    created_at: datetime
    updated_at: datetime
    evidence: list[EvidenceOut]


class CommitmentCreate(BaseModel):
    """FR-LED-10: manual creation, same evidence requirement as extracted
    commitments — the API layer supplies that evidence itself (the user
    action), so callers don't pass an evidence_span here."""

    party_id: uuid.UUID
    counterparty_id: uuid.UUID
    act_type: str
    deliverable_en: str
    deliverable_original: str | None = None
    deliverable_id: uuid.UUID | None = None
    due_at: datetime | None = None
    amount: float | None = None
    currency: str | None = None


class CommitmentCorrection(BaseModel):
    """FR-LED-08's 'correct if needed' step — every field optional, since
    most verifications correct nothing."""

    deliverable_en: str | None = None
    deliverable_original: str | None = None
    due_at: datetime | None = None
    amount: float | None = None
    currency: str | None = None


class VerifyRequest(BaseModel):
    corrections: CommitmentCorrection | None = None


class TransitionRequest(BaseModel):
    to_state: CommitmentStateLiteral
    reason: str | None = None


# --- Production Twin (PRD §6.8, FR-TWN) ---------------------------------


class MilestoneOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    type_term_id: uuid.UUID
    name: str
    planned_at: datetime | None
    actual_at: datetime | None
    is_fixed: bool
    created_at: datetime
    updated_at: datetime


class MilestoneCreate(BaseModel):
    """FR-TWN-01/10: a milestone the seeded archetype didn't anticipate —
    a client-specific approval gate, a step that only applies to this
    project. `type_code` matches ontology_terms.code (category=
    'milestone_type'), resolved against this project's effective vocabulary
    (app/twin/service.py's _resolve_ontology_terms) same as CommitmentCreate
    resolves `act_type` — never a raw type_term_id, so a caller never has to
    know or guess an internal id."""

    type_code: str
    name: str
    planned_at: datetime | None = None
    actual_at: datetime | None = None
    is_fixed: bool = False


class MilestoneUpdate(BaseModel):
    """FR-TWN-10: a PM override of any field here is recorded as an audited
    change (app/twin/audit.py), never a silent overwrite. Every field
    optional, since most overrides touch one thing."""

    name: str | None = None
    planned_at: datetime | None = None
    actual_at: datetime | None = None
    is_fixed: bool | None = None


class DependencyCreate(BaseModel):
    """FR-TWN-01/10: an edge the seeded archetype didn't have — e.g. wiring
    a newly added milestone into the existing graph. Rejected with 422 if it
    would make the graph cyclic (FR-TWN-01 requires a DAG), checked before
    insert."""

    upstream_milestone_id: uuid.UUID
    downstream_milestone_id: uuid.UUID
    lag_days: int = Field(default=0, ge=0)


class DependencyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    upstream_milestone_id: uuid.UUID
    downstream_milestone_id: uuid.UUID
    lag_days: int


class DependencyUpdate(BaseModel):
    """FR-TWN-10's "override of any duration" — `lag_days` is the only field
    that makes sense to override; the edge's endpoints are structural."""

    lag_days: int = Field(ge=0)


class TwinNodeOut(BaseModel):
    milestone_id: uuid.UUID
    earliest: datetime | None
    latest: datetime | None
    slack_days: float | None
    is_critical: bool


class TwinCurrentOut(BaseModel):
    """FR-TWN-03: current critical path, per-node slack, binding constraint."""

    nodes: list[TwinNodeOut]
    critical_path: list[uuid.UUID]
    binding_constraint: uuid.UUID | None


class TwinConstraintOut(BaseModel):
    binding_constraint: uuid.UUID | None
    slack_days: float | None


class PropagateCandidate(BaseModel):
    milestone_id: uuid.UUID
    new_date: datetime


class PropagateRequest(BaseModel):
    """FR-TWN-05/07: one or more hypothetical shifts to evaluate — a single
    candidate is the ordinary "what if X slips" case; more than one is how a
    PM compares recovery options side by side in one call (see
    app/twin/graph.py's propagate() docstring for why this endpoint, not a
    second one, is FR-TWN-07's surface)."""

    candidates: list[PropagateCandidate] = Field(min_length=1)


class AffectedNodeOut(BaseModel):
    milestone_id: uuid.UUID
    previous_earliest: datetime | None
    new_earliest: datetime | None
    consumed_slack_days: float | None
    propagation_stopped: bool


class PropagateCandidateResult(BaseModel):
    milestone_id: uuid.UUID
    new_date: datetime
    affected: list[AffectedNodeOut]
    binding_constraint_after: uuid.UUID | None


class PropagateResponse(BaseModel):
    results: list[PropagateCandidateResult]
