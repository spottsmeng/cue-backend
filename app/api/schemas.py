import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

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
