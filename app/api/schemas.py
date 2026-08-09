import uuid
from datetime import datetime, time
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

# app/models/channel_type.py's ChannelType is reference data (insertable
# rows), not a native enum, since the whole point is that a brand can be
# added without a code/schema change — so unlike the Literals around it,
# channel-type values are no longer a static Python type. Validity is
# checked at request time against the channel_types table instead (see
# app/api/channels.py's _resolve_channel_type); the wire type for both
# Channel.type (never "manual" — see _resolve_channel_type's
# require_capability flag) and Evidence.channel (genuinely can be "manual",
# FR-LED-10's "manually-entered-but-fully-real" posture) is plain `str`.

# Mirrors app/models/governance.py's ConsentStatus native enum.
ConsentStatusLiteral = Literal["pending", "accepted", "objected", "opted_out"]

# Mirrors app/documents/models.py's SpecClaimAttribute native enum.
SpecClaimAttributeLiteral = Literal["dimension", "finish", "quantity", "price"]

# Mirrors app/foresight/models.py's native enums (PRD §6.9/§6.7/§6.15).
RiskSourceLiteral = Literal["silence", "contradiction", "forecast"]
RiskSeverityLiteral = Literal["low", "medium", "high", "critical"]
RiskStatusLiteral = Literal["open", "acknowledged", "resolved", "superseded"]
DeviationStatusLiteral = Literal["auto_drafted", "confirmed", "resolved"]
NotificationChannelLiteral = Literal["webhook", "push", "email", "teams"]
ForesightThresholdMetricLiteral = Literal[
    "silence_multiplier", "escalation_hours", "forecast_slack_days"
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
    type: str
    external_ref: str | None
    healthy: bool
    created_at: datetime
    updated_at: datetime


class ChannelCreate(BaseModel):
    """FR-ADM-06's 'attach channels' step. `type` is validated against the
    channel_types table at request time, not a static Literal — see
    app/api/channels.py's _resolve_channel_type."""

    type: str
    external_ref: str | None = None


class ChannelTypeOut(BaseModel):
    """GET /channel-types — read-only discovery of valid `Channel.type` /
    `Evidence.channel` values, since they're no longer a closed Literal a
    client can introspect statically. Not the tenant-facing Configuration UI
    (no create/edit here) — just the read counterpart GET /projects/{id}/
    channels already established as acceptable scope for a discoverable
    resource."""

    model_config = ConfigDict(from_attributes=True)

    code: str
    capability: str | None
    active: bool


class ChannelHealthSignal(BaseModel):
    """FR-ADM-09: the receiving end of a capture agent's health report — no
    producer calls this yet (a later milestone's real capture agents will),
    but the shape is built now. `detail` is a free-form payload (e.g. last
    error, last successful poll) recorded for the caller's own diagnostics,
    not parsed here."""

    healthy: bool
    detail: dict | None = None


class ChannelHealthEventOut(BaseModel):
    """FR-CAP-09's durable health history (app/capture/models.py's
    ChannelHealthEvent, written by app/capture/health.py's
    run_capture_health_sweep) — the consumer surface
    backend/PROGRESS.md's M2 notes named as not yet existing ("no consumer
    of channel health history exists yet")."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    channel_id: uuid.UUID
    healthy: bool
    detail: dict | None
    checked_at: datetime


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


class ChannelIdentityOut(BaseModel):
    """FR-NRM-03: a resolved (channel_type, external_id) -> Party mapping —
    read surface over app/models/party.py's ChannelIdentity, for an
    Administrator reviewing low-confidence auto-resolutions."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    party_id: uuid.UUID
    channel_type: str
    external_id: str
    confidence: float
    manually_verified: bool
    created_at: datetime
    updated_at: datetime


class ChannelIdentityOverrideRequest(BaseModel):
    """FR-NRM-03's "manual override" — app/capture/identity.py's
    set_manual_identity_override. Upserts by (channel_type, external_id):
    an Administrator correcting either a brand-new mapping or one the
    resolver already auto-created at lower confidence."""

    channel_type: str
    external_id: str
    party_id: uuid.UUID


class PartyOrganisationMappingOut(BaseModel):
    """FR-NRM-04: one row of a person's effective-dated vendor-company
    history (app/parties/organisation_mapping.py)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    person_party_id: uuid.UUID
    organisation_party_id: uuid.UUID
    role_title: str | None
    effective_from: datetime
    effective_to: datetime | None


class PartyOrganisationMappingSet(BaseModel):
    """FR-NRM-04's write path — app/parties/organisation_mapping.py's
    set_current_organisation. `effective_from` defaults to now (a mapping
    discovered/declared today); pass an earlier timestamp to backdate a
    correction discovered after the fact."""

    organisation_party_id: uuid.UUID
    role_title: str | None = None
    effective_from: datetime | None = None


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
    channel: str = "manual"
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


# --- Foresight (PRD §6.9 FR-FOR / §6.7 FR-DEV / §6.15 FR-NTF) -----------


class RiskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    source: RiskSourceLiteral
    finding_key: str
    severity: RiskSeverityLiteral
    status: RiskStatusLiteral
    commitment_id: uuid.UUID | None
    milestone_id: uuid.UUID | None
    spec_claim_id: uuid.UUID | None
    downstream_consequence: str
    base_rate: float | None
    detail: dict
    acknowledged_by: uuid.UUID | None
    acknowledged_at: datetime | None
    resolved_at: datetime | None
    escalated_to_role: MembershipRoleLiteral | None
    escalated_at: datetime | None
    superseded_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class DeviationOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    class_term_id: uuid.UUID
    status: DeviationStatusLiteral
    description_en: str
    risk_id: uuid.UUID | None
    commitment_id: uuid.UUID | None
    milestone_id: uuid.UUID | None
    resolution_date: datetime | None
    resolution_owner: uuid.UUID | None
    confirmed_by: uuid.UUID | None
    confirmed_at: datetime | None
    detail: dict
    created_at: datetime
    updated_at: datetime
    evidence: list[EvidenceOut]


class DeviationCreate(BaseModel):
    """FR-DEV-01: manual entry. `original_text` is the caller's own
    account of the deviation — becomes this row's required Evidence, same
    "manually-entered-but-fully-real" posture FR-LED-10 established for
    CommitmentCreate."""

    class_code: str
    description_en: str
    commitment_id: uuid.UUID | None = None
    milestone_id: uuid.UUID | None = None
    original_text: str


class DeviationConfirmRequest(BaseModel):
    """FR-DEV-04's "PM confirms or edits" — an empty body confirms the
    auto-drafted row as-is; `description_en` set corrects it, mirroring
    VerifyRequest's corrections-optional shape. Doubles as this resource's
    "update" verb for an already-confirmed row (item 7) — there is no
    separate PATCH endpoint, since a confirm-with-corrections call already
    covers editing either kind of row identically."""

    description_en: str | None = None


class DeviationResolveRequest(BaseModel):
    resolution_date: datetime
    resolution_owner: uuid.UUID


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    recipient_id: uuid.UUID
    risk_id: uuid.UUID | None
    deviation_id: uuid.UUID | None
    commitment_id: uuid.UUID | None
    collapsed_risk_ids: list[uuid.UUID]
    collapsed_count: int
    severity: RiskSeverityLiteral
    downstream_consequence: str
    deliverable_at: datetime
    delivered_via: NotificationChannelLiteral | None
    sent_at: datetime | None
    acknowledged_by: uuid.UUID | None
    acknowledged_at: datetime | None
    created_at: datetime
    detail: dict


class WebhookSubscriptionOut(BaseModel):
    """Never carries `secret` — shown exactly once, at creation
    (WebhookSubscriptionCreated below), same "shown once" posture an API
    key would get; a lost secret means re-creating the subscription, not
    an endpoint that could leak it back out later."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    url: str
    event_types: list[str]
    active: bool
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime


class WebhookSubscriptionCreated(WebhookSubscriptionOut):
    secret: str


class WebhookSubscriptionCreate(BaseModel):
    url: str
    event_types: list[Literal["commitment", "risk", "deviation"]] = Field(min_length=1)


class ForesightThresholdOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organisation_id: uuid.UUID
    project_id: uuid.UUID | None
    deviation_class_term_id: uuid.UUID | None
    metric: ForesightThresholdMetricLiteral
    value: float
    created_at: datetime
    updated_at: datetime


class ForesightThresholdCreate(BaseModel):
    project_id: uuid.UUID | None = None
    deviation_class_term_id: uuid.UUID | None = None
    metric: ForesightThresholdMetricLiteral
    value: float


class ForesightThresholdUpdate(BaseModel):
    value: float


class QuietHoursConfigOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    quiet_start_local: time
    quiet_end_local: time
    critical_severity_threshold: RiskSeverityLiteral
    created_at: datetime
    updated_at: datetime


class QuietHoursConfigWrite(BaseModel):
    quiet_start_local: time
    quiet_end_local: time
    critical_severity_threshold: RiskSeverityLiteral = "critical"


# --- Ontology term discovery (app/api/ontology.py) ------------------------


class OntologyTermOut(BaseModel):
    """GET /projects/{id}/ontology-terms?category=X — read-only discovery
    of the valid *_code values for one ontology_terms category, resolved
    against the calling project's own effective three-tier vocabulary
    (CUE-PRD.md §4.2.1). `id` is deliberately omitted: every *_code field
    across this API (MilestoneCreate.type_code, CommitmentCreate.act_type,
    DeviationCreate.class_code, ...) takes the stable `code`, never the
    internal surrogate id — this response shape matches what a caller can
    actually use."""

    code: str
    label_en: str
    label_zh: str
    sort_order: int


# --- Party directory (app/api/parties.py) ---------------------------------

PartyTypeLiteral = Literal["person", "vendor_org", "internal_staff"]


class PartyOut(BaseModel):
    """GET /parties — org-wide vendor/contact directory. Party is org-
    scoped, not project-scoped (app/models/party.py's own docstring: "the
    same vendor contact is one row across every project they appear in"),
    same reasoning /parties/{id}/reliability already reads on."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organisation_id: uuid.UUID
    display_name: str
    type: PartyTypeLiteral
    vendor_category_term_id: uuid.UUID | None
    city: str | None
    created_at: datetime
    updated_at: datetime


# --- Cost accounting (app/api/admin.py, NFR-OBS-03) ------------------------


class CostSummaryRow(BaseModel):
    """One (project, provider, model) bucket from llm_usage_events
    (app/llm/cost.py). `estimated_cost_usd=None` on a row means a genuinely
    unrecognised model, not "free" — a self-hosted Ollama model reports a
    real 0.0 (app/llm/cost.py's own _PRICING_PER_1M_TOKENS comment), never
    None, so a caller can trust that distinction rather than treating every
    falsy cost the same way."""

    project_id: uuid.UUID | None
    provider: str
    model: str
    call_count: int
    tokens_in: int
    tokens_out: int
    estimated_cost_usd: float | None


class CostSummaryOut(BaseModel):
    """PRD §13's "cost per active project" row, made real for the first
    time — llm_usage_events has been written since the Hardening session
    (backend/PROGRESS.md's M10) but nothing ever read it back until this
    endpoint. `total_estimated_cost_usd=None` means literally no row in
    this result carried a known cost (e.g. every call so far was an
    unrecognised model), distinct from a real $0.00 across only-Ollama
    usage — same "don't fabricate what you don't have" discipline
    CostSummaryRow's own docstring applies at the row level."""

    rows: list[CostSummaryRow]
    total_calls: int
    total_estimated_cost_usd: float | None
