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


class ProjectCreate(BaseModel):
    name: str
    client_name: str | None = None
    venue: str | None = None
    timezone: str = "Asia/Singapore"
    vertical_code: str = "event-production"


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
