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


class EffectiveRoleOut(BaseModel):
    """Backs `GET /projects/{project_id}/members/me` — the frontend's own
    "which actions to *show* as available" judgment call
    (`app/api/deps.py`'s `require_project_role` docstring: "a UX nicety, not
    a security boundary"). `roles` is `app.identity.service.effective_roles`
    verbatim (own membership role plus any currently-active delegated
    role) — never itself checked by any mutating endpoint, every one of
    which independently re-derives and enforces this same set server-side."""

    roles: list[MembershipRoleLiteral]


class ProjectMemberOut(BaseModel):
    """Backs `GET /projects/{project_id}/members` — a project-scoped member
    directory any project member can read (same read tier as
    `GET .../milestones`), distinct from both `MembershipOut` above (raw
    `user_id`, no name/email — fine for an admin who already has a user in
    hand) and the org-wide, org-admin-only `GET /admin/roles`. Exists so a
    caller who needs to *name* a fellow member (e.g.
    `DeviationResolveRequest.resolution_owner`) can do so without either
    already knowing their UUID or holding org-admin access — never a
    security boundary of its own (any member can already infer their
    project's own roster from other project-scoped reads), only a name/
    email join FastAPI hasn't had a response shape for. Not
    `from_attributes`-backed: constructed from an explicit `Membership` JOIN
    `User` query (`app/api/projects.py`'s own list_project_members), same
    "no relationship(), one explicit select" style
    `app/api/deviations.py`'s `_to_out` already establishes for this
    codebase — Membership carries no ORM relationship to User to read
    display_name/email off of directly."""

    user_id: uuid.UUID
    display_name: str | None
    email: str
    role: MembershipRoleLiteral


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
    # Frontend-enablement addition (Prompt F6's own gap-audit check):
    # Project.archetype_code (app/models/project.py) is FR-VRG-02's own
    # "event archetype" segmentation axis, set once at materialize_archetype
    # time, but was never surfaced by any response schema before this —
    # `GET /parties/{id}/reliability`'s own `event_archetype` query param had
    # no way to discover which values are real for a caller's own projects.
    # Additive, no migration needed (the column already exists).
    archetype_code: str | None
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
    # FR-VOI-05: "retain original audio and make it playable from any
    # evidence link" — the model column (app/models/ledger.py) has carried
    # this since M8's capture pipeline set it (app/capture/pipeline.py), but
    # no response schema ever exposed it, so no API caller could ever play
    # a voice note back. A signed, expiring URI per that column's own
    # comment, or None when this evidence has no attached audio.
    media_ref: str | None
    # FR-VOI-04: per-message mean transcription confidence, set alongside
    # media_ref by the same voice-note pipeline (app/capture/pipeline.py) —
    # the same "column existed, no schema exposed it" gap one field later.
    # None for evidence with no attached voice note.
    transcript_confidence: float | None


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


class UserMeOut(BaseModel):
    """`GET/PATCH /users/me` — F9's own per-user preference surface
    (NFR-ACC-03). Deliberately not UserOut: this is "who am I and what are
    my own settings," reachable by any authenticated user about themselves,
    not the org-admin-gated directory listing."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    display_name: str | None
    high_contrast: bool


class UserPreferencesUpdate(BaseModel):
    high_contrast: bool


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


CommitmentSupersessionCandidateStatusLiteral = Literal["pending", "confirmed", "rejected"]


class CommitmentSupersessionCandidateOut(BaseModel):
    """FR-LED-05: an AI-proposed, not-yet-applied candidate link between two
    commitments (app/ledger/supersession.py) — plain ids only, no
    denormalised deliverable/amount text, same "resolve against an already-
    fetched list client-side" discipline this API holds everywhere else
    (OntologyTermOut, ProjectMemberOut) rather than duplicating display data
    that would drift from the commitments it describes."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    commitment_id: uuid.UUID
    supersedes_commitment_id: uuid.UUID
    reasoning: str
    status: CommitmentSupersessionCandidateStatusLiteral
    reviewed_by: uuid.UUID | None
    reviewed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ChannelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    type: str
    external_ref: str | None
    display_name: str | None
    healthy: bool
    detached_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ChannelCreate(BaseModel):
    """FR-ADM-06's 'attach channels' step. `type` is validated against the
    channel_types table at request time, not a static Literal — see
    app/api/channels.py's _resolve_channel_type.

    `external_ref` stays the one generic identifier field for every channel
    type ('Layer B Channel Picker' prompt's own design choice — a second,
    WhatsApp-only field would fork the shape for no real benefit): for
    `type="whatsapp"` the frontend picker now supplies the real JID it
    resolved from `GET .../channels/whatsapp/conversations`, never a
    hand-typed value; every other channel type keeps supplying it directly
    (mailbox address, drive id, ...), unchanged, since none of them has a
    discovery mechanism to pick from (out of scope this session).
    `display_name` is the picker's own resolved label, cached at attach
    time — see `Channel.display_name`'s own docstring
    (app/models/project.py) for why it isn't re-resolved live."""

    type: str
    external_ref: str | None = None
    display_name: str | None = None


class WhatsAppConversationOut(BaseModel):
    """`GET .../channels/whatsapp/conversations` — backs the attach-flow's
    real picker (`frontend/components/admin/project-channels-view.tsx`),
    proxying Layer A's live Machine API `GET /conversations`
    (`layer-A/src/api/machine/index.ts`) unmodified: real, WhatsApp-supplied
    names, discovered independently of capture status. `jid` is the opaque
    id a human never sees rendered directly — `name` is what the picker
    shows, `None` when Layer A hasn't resolved a real one yet (a contact
    whose display name/contact-sync event hasn't arrived) — the frontend
    falls back to a generic, still-jid-free label in that case, never the
    raw jid. `designated` reflects Layer A's own Level C allowlist state
    *before* this project's own attach action runs (e.g. already true if an
    operator manually added it through Layer A's ops console first)."""

    jid: str
    name: str | None
    kind: Literal["group", "contact"]
    designated: bool


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


class MessageOut(BaseModel):
    """A raw captured `Message` row (app/capture/models.py) — the debug
    console's own view over real capture, deliberately independent of
    extraction: a message is durably captured (NFR-AVL-02) whether or not
    `extraction_attempted_at` is set or ever produces a Commitment.
    `sender_external_id` is shown raw, not resolved to a Party display
    name — this is a debug tool for capture/identity itself, so the raw
    identity string the channel actually reported is more useful here than
    a polished label, same reasoning `ChannelIdentityOut` already shows raw
    `external_id` rather than hiding it."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    channel_id: uuid.UUID
    external_id: str
    sender_external_id: str
    author_party_id: uuid.UUID | None
    # FR-NRM-03's own "with confidence" — set by app/capture/identity.py's
    # resolve_identity at capture time, never previously exposed here. Lets
    # this same debug view double as the review surface for a low-confidence
    # match (see this round's PROGRESS.md note on why a dedicated filter/
    # sort endpoint wasn't added this round).
    identity_confidence: float | None
    identity_manually_verified: bool
    sent_at: datetime
    language: str | None
    text: str | None
    extraction_attempted_at: datetime | None


class CapturePullResponse(BaseModel):
    """`POST .../capture/pull-now`'s response. `queued=False` means arq's
    own per-channel dedup-by-job-id already had one in flight for this
    channel (app/capture/worker.py's own `enqueue_channel_ingestion`
    docstring) — not an error, just "already running, nothing new to do."
    """

    queued: bool


CaptureJobStatusLiteral = Literal["not_found", "deferred", "queued", "in_progress", "complete"]


class CaptureStatusOut(BaseModel):
    """`GET .../capture/status` — the real arq job state behind a prior
    `POST .../capture/pull-now`, keyed by the same deterministic per-channel
    job id (`app/capture/worker.py`'s `channel_job_id`) arq's own dedup
    already uses. `status` mirrors arq's own `JobStatus` enum values.
    `not_found` covers two genuinely different real cases a caller can't
    tell apart from this alone: no pull has ever been queued for this
    channel, or one completed long enough ago that arq's own result TTL
    (`keep_result`, default 1 hour) already expired it — both render the
    same "nothing to show" way client-side, so the ambiguity is harmless.

    `success`/`error` and `received`.../`latest_sent_at` are only ever
    populated once `status="complete"`: `success=False` with `error` set
    means the job itself raised (a real failure, `error` is `str(exception)`,
    never a raw traceback); `success=True` with the count fields set is
    `_summary_dict`'s own real return shape (app/capture/worker.py's
    `IngestionSummary`), not reconstructed here. `skipped`/`skip_reason`
    cover the job's own early-exit case (channel or project deleted between
    enqueue and pickup) — a real, successful completion, just with nothing
    captured, distinct from either the count-bearing success case or a
    genuine failure."""

    status: CaptureJobStatusLiteral
    success: bool | None = None
    error: str | None = None
    skipped: bool | None = None
    skip_reason: str | None = None
    received: int | None = None
    new_messages: int | None = None
    duplicates: int | None = None
    opted_out: int | None = None
    commitments_created: int | None = None
    latest_sent_at: datetime | None = None


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


DocumentAuditActionLiteral = Literal[
    "document_created", "version_created", "version_approved", "auto_tagged", "project_archived"
]


class DocumentAuditLogOut(BaseModel):
    """The Documents page's own Activity tab — one document's real
    `DocumentAuditLog` trail (app/documents/models.py), previously reachable
    only through `/admin/export`'s whole-project bundle. `document_id` is
    always this document's own id here (the endpoint filters on it); it's
    still on the response because `project_archived` rows share this same
    model with `document_id` NULL — a client that ever aggregates rows from
    more than one fetch needs to be able to tell them apart."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    document_id: uuid.UUID | None
    document_version_id: uuid.UUID | None
    action: DocumentAuditActionLiteral
    actor_id: uuid.UUID | None
    occurred_at: datetime
    detail: dict


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
    # The extraction model's own 0-1 confidence in this claim
    # (app/documents/schema.py's ExtractedSpecClaim) — the column has
    # carried it since app/documents/models.py's SpecClaim.confidence, no
    # response schema exposed it until now. None for a manually-entered
    # claim with no model confidence to report.
    confidence: float | None
    evidence: list[EvidenceOut]


class DocumentSearchResult(BaseModel):
    document: DocumentOut
    version: DocumentVersionOut
    rank: float


class SpecClaimResolvedOut(SpecClaimOut):
    """F4 frontend-enablement addition: `SpecClaim.contradicts` can point at
    a claim on a *different* document version — Foresight's own
    contradiction detector (app/foresight/contradiction.py) compares claims
    project-wide by shared deliverable_id/location_code, not just within one
    version. `GET .../versions/{id}/spec-claims` only returns claims for one
    version, so a `contradicts` target outside that list is otherwise an
    unresolvable UUID with no way to reach its own document. This adds just
    enough document identity (id, name, the version's own number) to render
    "conflicts with <attribute>=<value> at <location_code>, from <document
    name>" and link out to it — doesn't touch SpecClaim's own field set,
    which CUE-PRD.md §4.3 already fixed."""

    document_id: uuid.UUID
    document_name: str
    document_version_no: int


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
    (CUE-PRD.md §4.2.1). Every *_code field across this API
    (MilestoneCreate.type_code, CommitmentCreate.act_type,
    DeviationCreate.class_code, ...) still takes the stable `code`, never
    `id` — that part of the original design holds.

    `id` was dropped from the first version of this response on exactly that
    reasoning, but it silently broke a *different, equally real* need this
    session (frontend F3) hit and F2 had already independently hit before
    it: resolving an already-persisted `*_term_id` FK (MilestoneOut.
    type_term_id, DeviationOut.class_term_id, ...) back to a human-readable
    label. A caller can't join a stored id against a response keyed by code
    alone. `id` is additive here (no existing caller reads a fixed key set
    or would break on a new field), so both needs are served by the same
    endpoint rather than adding a second one."""

    id: uuid.UUID
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
