import uuid
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_actor_id, get_project, require_project_role
from app.api.schemas import (
    CommitmentCreate,
    CommitmentOut,
    CommitmentStateLiteral,
    EvidenceOut,
    TransitionRequest,
    VerifyRequest,
)
from app.core.db import get_session
from app.identity.service import WRITE_ROLES
from app.ledger.audit import record_audit_event
from app.ledger.extractor import _get_commitment_act_term
from app.ledger.lifecycle import InvalidTransition, validate_transition
from app.models import Commitment, Deliverable, Evidence, Party, Project
from app.twin.service import recompute_on_commitment_transition

_require_write = require_project_role(*WRITE_ROLES)

router = APIRouter(prefix="/projects/{project_id}/commitments", tags=["commitments"])


async def _get_commitment(
    session: AsyncSession, project: Project, commitment_id: uuid.UUID
) -> Commitment:
    commitment = (
        await session.execute(
            select(Commitment).where(
                Commitment.id == commitment_id, Commitment.project_id == project.id
            )
        )
    ).scalar_one_or_none()
    if commitment is None:
        raise HTTPException(status_code=404, detail="commitment not found")
    return commitment


async def _require_party(
    session: AsyncSession, organisation_id: uuid.UUID, party_id: uuid.UUID, field: str
) -> None:
    """`parties` has no RLS policy of its own (only organisations/projects/
    commitments do — see alembic/versions/a895ae03ec5c), so this
    organisation_id filter is the only thing standing between a caller and
    referencing another tenant's party by guessing its id. Without it, a bad
    or cross-tenant party_id would only be caught by the FK constraint at
    INSERT time, surfacing as a raw 500 instead of a clean 422."""
    exists = (
        await session.execute(
            select(Party.id).where(Party.id == party_id, Party.organisation_id == organisation_id)
        )
    ).scalar_one_or_none()
    if exists is None:
        raise HTTPException(status_code=422, detail=f"{field}: no such party {party_id}")


async def _require_deliverable(
    session: AsyncSession, project_id: uuid.UUID, deliverable_id: uuid.UUID | None
) -> None:
    if deliverable_id is None:
        return
    exists = (
        await session.execute(
            select(Deliverable.id).where(
                Deliverable.id == deliverable_id, Deliverable.project_id == project_id
            )
        )
    ).scalar_one_or_none()
    if exists is None:
        raise HTTPException(
            status_code=422, detail=f"deliverable_id: no such deliverable {deliverable_id}"
        )


async def _refresh_updated_at(session: AsyncSession, commitment: Commitment) -> None:
    """`updated_at` has a server-side `onupdate=func.now()` — after an UPDATE
    flush, SQLAlchemy expires it (it can't know the server-computed value
    without asking) rather than fetching it eagerly the way it does for a
    fresh INSERT's server_default. A bare attribute access after that would
    silently try a lazy-load SELECT outside the async greenlet bridge and
    raise MissingGreenlet, so refresh it explicitly, before commit (while
    app.current_org_id, is_local=true, is still set for this transaction —
    see app/core/db.py's get_session docstring)."""
    await session.refresh(commitment, attribute_names=["updated_at"])


def _jsonable(value: object) -> object:
    """detail/JSONB columns go through stdlib json.dumps, which chokes on
    Decimal (Commitment.amount's Python-side type) and doesn't touch
    datetime either — used only for the audit trail's before/after values."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


async def _to_out(session: AsyncSession, commitment: Commitment) -> CommitmentOut:
    """No relationship() on these models (the codebase's established style —
    see _get_or_create_party in extractor.py for the same explicit-select
    pattern), so evidence is loaded with its own query rather than an ORM
    join/eager-load."""
    evidence_rows = (
        (await session.execute(select(Evidence).where(Evidence.commitment_id == commitment.id)))
        .scalars()
        .all()
    )
    return CommitmentOut(
        id=commitment.id,
        project_id=commitment.project_id,
        party_id=commitment.party_id,
        counterparty_id=commitment.counterparty_id,
        deliverable_id=commitment.deliverable_id,
        act_type_id=commitment.act_type_id,
        state=commitment.state,
        deliverable_en=commitment.deliverable_en,
        deliverable_original=commitment.deliverable_original,
        due_at=commitment.due_at,
        amount=commitment.amount,
        currency=commitment.currency,
        payment_status=commitment.payment_status,
        confidence=commitment.confidence,
        field_confidence=commitment.field_confidence,
        verification_state=commitment.verification_state,
        verified_by=commitment.verified_by,
        verified_at=commitment.verified_at,
        created_at=commitment.created_at,
        updated_at=commitment.updated_at,
        evidence=[EvidenceOut.model_validate(e) for e in evidence_rows],
    )


@router.get("", response_model=list[CommitmentOut])
async def list_commitments(
    project: Annotated[Project, Depends(get_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
    state: CommitmentStateLiteral | None = None,
    party_id: uuid.UUID | None = None,
    due_before: datetime | None = None,
    due_after: datetime | None = None,
) -> list[CommitmentOut]:
    """PRD §11.2: list filtered by state, party and due window."""
    stmt = select(Commitment).where(Commitment.project_id == project.id)
    if state is not None:
        stmt = stmt.where(Commitment.state == state)
    if party_id is not None:
        stmt = stmt.where(Commitment.party_id == party_id)
    if due_before is not None:
        stmt = stmt.where(Commitment.due_at <= due_before)
    if due_after is not None:
        stmt = stmt.where(Commitment.due_at >= due_after)
    stmt = stmt.order_by(Commitment.due_at.asc().nulls_last())

    commitments = (await session.execute(stmt)).scalars().all()
    return [await _to_out(session, c) for c in commitments]


@router.get("/{commitment_id}", response_model=CommitmentOut)
async def read_commitment(
    commitment_id: uuid.UUID,
    project: Annotated[Project, Depends(get_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CommitmentOut:
    commitment = await _get_commitment(session, project, commitment_id)
    return await _to_out(session, commitment)


@router.post("", response_model=CommitmentOut, status_code=201)
async def create_commitment(
    body: CommitmentCreate,
    project: Annotated[Project, Depends(_require_write)],
    session: Annotated[AsyncSession, Depends(get_session)],
    actor_id: Annotated[uuid.UUID, Depends(get_actor_id)],
) -> CommitmentOut:
    """FR-LED-10: manual creation, with the same evidence requirement as
    extracted commitments — satisfied here by an Evidence row that names the
    user action itself as the source, not a message span."""
    try:
        act_term = await _get_commitment_act_term(session, body.act_type)
    except LookupError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    await _require_party(session, project.organisation_id, body.party_id, "party_id")
    await _require_party(session, project.organisation_id, body.counterparty_id, "counterparty_id")
    await _require_deliverable(session, project.id, body.deliverable_id)

    commitment = Commitment(
        project_id=project.id,
        party_id=body.party_id,
        counterparty_id=body.counterparty_id,
        deliverable_id=body.deliverable_id,
        act_type_id=act_term.id,
        state="proposed",
        deliverable_en=body.deliverable_en,
        deliverable_original=body.deliverable_original or body.deliverable_en,
        due_at=body.due_at,
        amount=body.amount,
        currency=body.currency,
        confidence=1.0,
        field_confidence={},
        # FR-LED-07's routing applies to manual entry too, not just extraction.
        verification_state=(
            "pending_verification" if (body.amount is not None or body.due_at is not None) else "auto"
        ),
    )
    session.add(commitment)
    await session.flush()  # need commitment.id for the evidence row below

    evidence = Evidence(
        commitment_id=commitment.id,
        channel="manual",
        sent_at=datetime.now(dt_timezone.utc),
        language="en",
        original_text=f"Manually entered via the CUE API by user {actor_id} (FR-LED-10).",
    )
    session.add(evidence)
    await session.flush()

    await record_audit_event(
        session,
        project_id=project.id,
        commitment_id=commitment.id,
        action="created",
        actor_id=actor_id,
        to_state=commitment.state,
        evidence_id=evidence.id,
    )
    await session.commit()
    return await _to_out(session, commitment)


@router.post("/{commitment_id}/verify", response_model=CommitmentOut)
async def verify_commitment(
    commitment_id: uuid.UUID,
    body: VerifyRequest,
    project: Annotated[Project, Depends(_require_write)],
    session: Annotated[AsyncSession, Depends(get_session)],
    actor_id: Annotated[uuid.UUID, Depends(get_actor_id)],
) -> CommitmentOut:
    """FR-LED-08: three-tap verification. 'View evidence' is
    GET /{commitment_id} (evidence[] is always in that response, per §11.1);
    this endpoint is 'correct if needed' + 'confirm' as one call."""
    commitment = await _get_commitment(session, project, commitment_id)

    changes: dict[str, dict[str, object]] = {}
    if body.corrections is not None:
        for field, new_value in body.corrections.model_dump(exclude_unset=True).items():
            old_value = getattr(commitment, field)
            if new_value != old_value:
                changes[field] = {"before": _jsonable(old_value), "after": _jsonable(new_value)}
                setattr(commitment, field, new_value)

    # FR-LED-09: a correction is labelled training signal, distinct from a
    # plain confirmation — verification_state and the audit action both
    # record which one this was.
    commitment.verification_state = "human_corrected" if changes else "human_verified"
    commitment.verified_by = actor_id
    commitment.verified_at = datetime.now(dt_timezone.utc)
    await session.flush()
    await _refresh_updated_at(session, commitment)

    await record_audit_event(
        session,
        project_id=project.id,
        commitment_id=commitment.id,
        action="corrected" if changes else "verified",
        actor_id=actor_id,
        detail={"changes": changes} if changes else {},
    )
    await session.commit()
    return await _to_out(session, commitment)


@router.post("/{commitment_id}/transitions", response_model=CommitmentOut)
async def transition_commitment(
    commitment_id: uuid.UUID,
    body: TransitionRequest,
    project: Annotated[Project, Depends(_require_write)],
    session: Annotated[AsyncSession, Depends(get_session)],
    actor_id: Annotated[uuid.UUID, Depends(get_actor_id)],
) -> CommitmentOut:
    """FR-LCY-01. Manual/API-driven transitions only — automatic transitions
    from silence/upstream-slip/forecast-breach (FR-LCY-02/03) depend on
    Twin/Foresight infrastructure this session doesn't build."""
    commitment = await _get_commitment(session, project, commitment_id)
    from_state = commitment.state

    try:
        validate_transition(from_state, body.to_state)
    except InvalidTransition as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    commitment.state = body.to_state
    try:
        await session.flush()  # the DB trigger checks this too — defense in depth, see app/ledger/lifecycle.py
    except DBAPIError as e:
        await session.rollback()
        raise HTTPException(status_code=409, detail="invalid commitment state transition") from e
    await _refresh_updated_at(session, commitment)

    await record_audit_event(
        session,
        project_id=project.id,
        commitment_id=commitment.id,
        action="state_transition",
        actor_id=actor_id,
        from_state=from_state,
        to_state=body.to_state,
        detail={"reason": body.reason} if body.reason else {},
    )
    # FR-TWN-04: recompute the Twin within the same transaction/request when
    # this transition's deliverable resolves to a milestone whose date is
    # affected — see app/twin/service.py's recompute_on_commitment_transition
    # for exactly what "affected" means here.
    await recompute_on_commitment_transition(
        session,
        project_id=project.id,
        commitment=commitment,
        to_state=body.to_state,
        actor_id=actor_id,
    )
    await session.commit()
    return await _to_out(session, commitment)
