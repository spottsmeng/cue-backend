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

# Mirrors app/models/project.py's ChannelType native enum, minus "manual" —
# that value exists for Evidence.channel's "not captured off a channel at
# all" case (FR-LED-10), never a meaningful type for an attachable Channel.
ChannelTypeLiteral = Literal["whatsapp", "wechat", "teams", "outlook", "sharepoint"]

# Mirrors app/models/governance.py's ConsentStatus native enum.
ConsentStatusLiteral = Literal["pending", "accepted", "objected", "opted_out"]

# Mirrors app/models/project.py's ChannelType native enum, *including*
# "manual" — unlike ChannelTypeLiteral above (Channel-the-resource can never
# be "manual"), Evidence.channel genuinely can be, and a document's evidence
# is exactly an Evidence row (FR-LED-10's "manually-entered-but-fully-real"
# posture, reused here for FR-DOC-01 ingestion).
EvidenceChannelLiteral = Literal["whatsapp", "wechat", "teams", "outlook", "sharepoint", "manual"]

# Mirrors app/documents/models.py's SpecClaimAttribute native enum.
SpecClaimAttributeLiteral = Literal["dimension", "finish", "quantity", "price"]


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


# --- Governance completion (PRD §6.14, FR-ADM) -------------------------


class UserOut(BaseModel):
    """§11.2's /admin/users — org-wide, not the project-scoped MembershipOut
    above. No SCIM pre-provisioning this session (out of scope, per the
    identity/RBAC session's own note), so this only ever lists users who
    have actually authenticated at least once."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organisation_id: uuid.UUID
    email: str
    display_name: str | None
    active: bool
    created_at: datetime
    updated_at: datetime


class BudgetOut(BaseModel):
    """PRD §4.3's Budget schema."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    approved_amount: float
    currency: str
    approved_by: uuid.UUID
    approved_at: datetime
    revision_of: uuid.UUID | None
    is_current: bool


class BudgetWrite(BaseModel):
    """Shared by both the initial baseline (POST) and a revision (POST
    .../revise) — same fields either way. No `evidence` input: the API layer
    always supplies it itself (an Evidence row naming the approving user's
    own action), same pattern app/api/commitments.py's create_commitment
    already uses for manually-entered commitments."""

    approved_amount: float
    currency: str = Field(min_length=3, max_length=3)


class PaymentStatusUpdate(BaseModel):
    """FR-LED-13: deliberately its own request body, not
    CommitmentCorrection — payment_status must never be settable through the
    general /verify endpoint that handles model-extracted fields."""

    payment_status: PaymentStatusLiteral


class ChannelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    type: ChannelTypeLiteral
    external_ref: str | None
    healthy: bool
    created_at: datetime
    updated_at: datetime


class ChannelCreate(BaseModel):
    """FR-ADM-06's 'attach channels' step."""

    type: ChannelTypeLiteral
    external_ref: str | None = None


class ChannelHealthSignal(BaseModel):
    """FR-ADM-09: the receiving end of a capture agent's health report — no
    producer calls this yet (a later milestone's real capture agents will),
    but the shape is built now. `detail` is a free-form payload (e.g. last
    error, last successful poll) recorded for the caller's own diagnostics,
    not parsed here."""

    healthy: bool
    detail: dict | None = None


class ConsentRecordOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    party_id: uuid.UUID
    project_id: uuid.UUID
    notice_sent_at: datetime | None
    status: ConsentStatusLiteral
    evidence: str | None
    created_at: datetime
    updated_at: datetime


class ConsentActionRequest(BaseModel):
    """FR-ADM-07's 'action data-subject requests' — records or updates a
    party's consent status on a project. Upsert-by-(party_id, project_id),
    same idempotent-by-natural-key shape app/api/projects.py's
    _grant_membership already uses for Membership."""

    party_id: uuid.UUID
    project_id: uuid.UUID
    status: ConsentStatusLiteral
    evidence: str | None = None
    notice_sent_at: datetime | None = None


class RetentionPolicyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organisation_id: uuid.UUID
    vertical_id: uuid.UUID | None
    region: str | None
    retention_days: int
    created_at: datetime
    updated_at: datetime


class RetentionPolicyCreate(BaseModel):
    vertical_id: uuid.UUID | None = None
    region: str | None = None
    retention_days: int = Field(gt=0)


class RetentionPolicyUpdate(BaseModel):
    """Every field optional — most edits touch one axis or the window alone."""

    vertical_id: uuid.UUID | None = None
    region: str | None = None
    retention_days: int | None = Field(default=None, gt=0)


class ProjectArchiveOut(BaseModel):
    """POST .../archive's response — the project itself plus what
    FR-DOC-06's retention-policy application actually resolved to, since
    there's no deletion scheduler to hand that resolution to yet
    (app/documents/service.py's archive_project docstring)."""

    project: ProjectOut
    retention_policy_id: uuid.UUID | None
    retention_days: int | None


# --- Documents (PRD §6.6, FR-DOC; §4.3's Spec Claim schema) ------------


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    class_term_id: uuid.UUID | None
    milestone_type_term_id: uuid.UUID | None
    phase_term_id: uuid.UUID | None
    current_version_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class DocumentVersionOut(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    version_no: int
    storage_ref: str
    extracted_text: str | None
    approved_by: uuid.UUID | None
    approved_at: datetime | None
    is_current: bool
    download_url: str | None
    created_at: datetime
    updated_at: datetime


class DocumentLineageOut(BaseModel):
    """FR-DOC-02/04: the full version history of one document, in order,
    with the currently-approved-and-authoritative version unambiguously
    flagged (each DocumentVersionOut's own `is_current`, and repeated here
    at the top level for a client that only wants the pointer)."""

    document_id: uuid.UUID
    current_version_id: uuid.UUID | None
    versions: list[DocumentVersionOut]


class DocumentCreate(BaseModel):
    """FR-DOC-01 ingestion — manual/API upload, same "manually-entered-but-
    fully-real" posture Ledger established for FR-LED-10: the caller
    supplies the file and its own evidence, never a simulated one. Sent as
    multipart/form-data (app/api/documents.py's create_document endpoint),
    not JSON — this model documents the field set but isn't bound directly
    to the request body."""

    name: str
    class_code: str | None = None
    channel: EvidenceChannelLiteral = "manual"
    sent_at: datetime | None = None
    language: str = "en"
    original_text: str | None = None
    translation: str | None = None
    extracted_text: str | None = None


class DocumentTagRequest(BaseModel):
    """FR-DOC-03. Every field optional — a caller may tag one axis without
    disturbing the others already set."""

    class_code: str | None = None
    milestone_type_code: str | None = None
    phase_code: str | None = None


class SpecClaimOut(BaseModel):
    id: uuid.UUID
    document_version_id: uuid.UUID
    deliverable_id: uuid.UUID | None
    location_code: str | None
    attribute: SpecClaimAttributeLiteral
    value: str
    contradicts: uuid.UUID | None
    evidence: list[EvidenceOut]


class DocumentSearchResult(BaseModel):
    document: DocumentOut
    version: DocumentVersionOut
    rank: float
