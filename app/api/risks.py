import uuid
from datetime import datetime, timezone as dt_timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_actor_id, get_project, require_project_role
from app.api.schemas import RiskOut, RiskSeverityLiteral, RiskSourceLiteral, RiskStatusLiteral
from app.core.db import get_session
from app.foresight.audit import record_foresight_audit_event
from app.foresight.models import Risk
from app.identity.service import WRITE_ROLES
from app.models import Project

_require_write = require_project_role(*WRITE_ROLES)

router = APIRouter(prefix="/projects/{project_id}/risks", tags=["foresight"])


async def _get_risk(session: AsyncSession, project: Project, risk_id: uuid.UUID) -> Risk:
    risk = (
        await session.execute(select(Risk).where(Risk.id == risk_id, Risk.project_id == project.id))
    ).scalar_one_or_none()
    if risk is None:
        raise HTTPException(status_code=404, detail="risk not found")
    return risk


@router.get("", response_model=list[RiskOut])
async def list_risks(
    project: Annotated[Project, Depends(get_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
    status: RiskStatusLiteral | None = None,
    source: RiskSourceLiteral | None = None,
    severity: RiskSeverityLiteral | None = None,
) -> list[Risk]:
    stmt = select(Risk).where(Risk.project_id == project.id)
    if status is not None:
        stmt = stmt.where(Risk.status == status)
    if source is not None:
        stmt = stmt.where(Risk.source == source)
    if severity is not None:
        stmt = stmt.where(Risk.severity == severity)
    stmt = stmt.order_by(Risk.created_at.desc())
    return list((await session.execute(stmt)).scalars().all())


@router.get("/{risk_id}", response_model=RiskOut)
async def read_risk(
    risk_id: uuid.UUID,
    project: Annotated[Project, Depends(get_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Risk:
    return await _get_risk(session, project, risk_id)


@router.post("/{risk_id}/acknowledge", response_model=RiskOut)
async def acknowledge_risk(
    risk_id: uuid.UUID,
    project: Annotated[Project, Depends(_require_write)],
    session: Annotated[AsyncSession, Depends(get_session)],
    actor_id: Annotated[uuid.UUID, Depends(get_actor_id)],
) -> Risk:
    """FR-NTF-05: acknowledging a Risk is what stops
    app/foresight/escalation.py's sweep from escalating it further."""
    risk = await _get_risk(session, project, risk_id)
    if risk.status not in ("open", "acknowledged"):
        raise HTTPException(status_code=409, detail=f"cannot acknowledge a {risk.status} risk")
    risk.status = "acknowledged"
    risk.acknowledged_by = actor_id
    risk.acknowledged_at = datetime.now(dt_timezone.utc)
    await session.flush()
    await session.refresh(risk, attribute_names=["updated_at"])

    await record_foresight_audit_event(
        session, project_id=project.id, action="risk_acknowledged", actor_id=actor_id, risk_id=risk.id,
    )
    await session.commit()
    return risk


@router.post("/{risk_id}/resolve", response_model=RiskOut)
async def resolve_risk(
    risk_id: uuid.UUID,
    project: Annotated[Project, Depends(_require_write)],
    session: Annotated[AsyncSession, Depends(get_session)],
    actor_id: Annotated[uuid.UUID, Depends(get_actor_id)],
) -> Risk:
    """A PM marking the underlying condition genuinely cleared — distinct
    from `superseded` (app/foresight/risk.py's own dedup mechanism, which
    only ever fires from a detector, never from a human action)."""
    risk = await _get_risk(session, project, risk_id)
    if risk.status not in ("open", "acknowledged"):
        raise HTTPException(status_code=409, detail=f"cannot resolve a {risk.status} risk")
    risk.status = "resolved"
    risk.resolved_at = datetime.now(dt_timezone.utc)
    await session.flush()
    await session.refresh(risk, attribute_names=["updated_at"])

    await record_foresight_audit_event(
        session, project_id=project.id, action="risk_resolved", actor_id=actor_id, risk_id=risk.id,
    )
    await session.commit()
    return risk
