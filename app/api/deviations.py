import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_actor_id, get_project, require_project_role
from app.api.schemas import (
    DeviationConfirmRequest,
    DeviationCreate,
    DeviationOut,
    DeviationResolveRequest,
    DeviationStatusLiteral,
    EvidenceOut,
)
from app.core.db import get_session
from app.foresight.deviation import (
    UnknownDeviationClass,
    confirm_deviation,
    create_manual_deviation,
    resolve_deviation,
)
from app.foresight.models import Deviation
from app.identity.service import WRITE_ROLES
from app.models import Evidence, Project

_require_write = require_project_role(*WRITE_ROLES)

router = APIRouter(prefix="/projects/{project_id}/deviations", tags=["foresight"])


async def _get_deviation(session: AsyncSession, project: Project, deviation_id: uuid.UUID) -> Deviation:
    deviation = (
        await session.execute(
            select(Deviation).where(Deviation.id == deviation_id, Deviation.project_id == project.id)
        )
    ).scalar_one_or_none()
    if deviation is None:
        raise HTTPException(status_code=404, detail="deviation not found")
    return deviation


async def _to_out(session: AsyncSession, deviation: Deviation) -> DeviationOut:
    """No relationship() on these models — evidence loaded with its own
    query, same explicit-select style app/api/commitments.py's _to_out
    already establishes."""
    evidence_rows = (
        (await session.execute(select(Evidence).where(Evidence.deviation_id == deviation.id)))
        .scalars()
        .all()
    )
    return DeviationOut(
        id=deviation.id,
        project_id=deviation.project_id,
        class_term_id=deviation.class_term_id,
        status=deviation.status,
        description_en=deviation.description_en,
        risk_id=deviation.risk_id,
        commitment_id=deviation.commitment_id,
        milestone_id=deviation.milestone_id,
        resolution_date=deviation.resolution_date,
        resolution_owner=deviation.resolution_owner,
        confirmed_by=deviation.confirmed_by,
        confirmed_at=deviation.confirmed_at,
        detail=deviation.detail,
        created_at=deviation.created_at,
        updated_at=deviation.updated_at,
        evidence=[EvidenceOut.model_validate(e) for e in evidence_rows],
    )


@router.get("", response_model=list[DeviationOut])
async def list_deviations(
    project: Annotated[Project, Depends(get_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
    status: DeviationStatusLiteral | None = None,
) -> list[DeviationOut]:
    """FR-DEV-05: this listing is what a progress report's risk/issues log
    rolls up from — the reporting milestone (Prompt 8) reads this endpoint
    rather than querying the table directly."""
    stmt = select(Deviation).where(Deviation.project_id == project.id)
    if status is not None:
        stmt = stmt.where(Deviation.status == status)
    stmt = stmt.order_by(Deviation.created_at.desc())
    deviations = (await session.execute(stmt)).scalars().all()
    return [await _to_out(session, d) for d in deviations]


@router.get("/{deviation_id}", response_model=DeviationOut)
async def read_deviation(
    deviation_id: uuid.UUID,
    project: Annotated[Project, Depends(get_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DeviationOut:
    deviation = await _get_deviation(session, project, deviation_id)
    return await _to_out(session, deviation)


@router.post("", response_model=DeviationOut, status_code=201)
async def create_deviation(
    body: DeviationCreate,
    project: Annotated[Project, Depends(_require_write)],
    session: Annotated[AsyncSession, Depends(get_session)],
    actor_id: Annotated[uuid.UUID, Depends(get_actor_id)],
) -> DeviationOut:
    """FR-DEV-01: manual entry — a PM logging a scope change/delay/
    corrective action directly, alongside the auto-drafted path
    (app/foresight/deviation.py's draft_deviation, called from the
    detectors)."""
    try:
        deviation = await create_manual_deviation(
            session,
            project=project,
            actor_id=actor_id,
            class_code=body.class_code,
            description_en=body.description_en,
            commitment_id=body.commitment_id,
            milestone_id=body.milestone_id,
            original_text=body.original_text,
        )
    except UnknownDeviationClass as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    await session.commit()
    return await _to_out(session, deviation)


@router.post("/{deviation_id}/confirm", response_model=DeviationOut)
async def confirm_deviation_endpoint(
    deviation_id: uuid.UUID,
    body: DeviationConfirmRequest,
    project: Annotated[Project, Depends(_require_write)],
    session: Annotated[AsyncSession, Depends(get_session)],
    actor_id: Annotated[uuid.UUID, Depends(get_actor_id)],
) -> DeviationOut:
    """FR-DEV-04's "PM confirms or edits", and this resource's "update"
    verb (DeviationConfirmRequest's own docstring)."""
    deviation = await _get_deviation(session, project, deviation_id)
    corrections = {"description_en": body.description_en} if body.description_en is not None else None
    deviation = await confirm_deviation(
        session, deviation=deviation, actor_id=actor_id, corrections=corrections
    )
    await session.commit()
    return await _to_out(session, deviation)


@router.post("/{deviation_id}/resolve", response_model=DeviationOut)
async def resolve_deviation_endpoint(
    deviation_id: uuid.UUID,
    body: DeviationResolveRequest,
    project: Annotated[Project, Depends(_require_write)],
    session: Annotated[AsyncSession, Depends(get_session)],
    actor_id: Annotated[uuid.UUID, Depends(get_actor_id)],
) -> DeviationOut:
    """FR-DEV-03: resolution date + owner."""
    deviation = await _get_deviation(session, project, deviation_id)
    if deviation.status == "resolved":
        raise HTTPException(status_code=409, detail="deviation is already resolved")
    deviation = await resolve_deviation(
        session,
        deviation=deviation,
        actor_id=actor_id,
        resolution_date=body.resolution_date,
        resolution_owner=body.resolution_owner,
    )
    await session.commit()
    return await _to_out(session, deviation)
