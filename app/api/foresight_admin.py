import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_project, require_org_administrator, require_project_role
from app.api.schemas import (
    ForesightThresholdCreate,
    ForesightThresholdOut,
    ForesightThresholdUpdate,
    QuietHoursConfigOut,
    QuietHoursConfigWrite,
)
from app.core.db import get_session
from app.identity.models import User
from app.foresight.models import ForesightThreshold, QuietHoursConfig
from app.identity.service import WRITE_ROLES
from app.models import OntologyTerm, Project

# FR-FOR-07: per-project, per-deviation-class thresholds — same
# org-admin-gated, RetentionPolicy-style config surface app/api/retention.py
# already establishes (app/foresight/models.py's ForesightThreshold
# docstring: "extends... rather than inventing a parallel mechanism").
threshold_router = APIRouter(prefix="/admin/foresight-thresholds", tags=["foresight"])

# FR-NTF-04's quiet-hours config is project-scoped (QuietHoursConfig's own
# docstring), so it's write-role-gated like ordinary project configuration
# (app/api/channels.py's precedent), not org-admin-gated.
_require_write = require_project_role(*WRITE_ROLES)
quiet_hours_router = APIRouter(prefix="/projects/{project_id}/quiet-hours", tags=["foresight"])


async def _get_threshold(session: AsyncSession, threshold_id: uuid.UUID) -> ForesightThreshold:
    """No explicit organisation_id filter — foresight_thresholds' own
    tenant_isolation policy (direct-column, migration 45309b1d751f, same
    shape as retention_policies) already confines this to the caller's
    organisation, same reasoning app/api/retention.py's _get_policy gives."""
    threshold = (
        await session.execute(select(ForesightThreshold).where(ForesightThreshold.id == threshold_id))
    ).scalar_one_or_none()
    if threshold is None:
        raise HTTPException(status_code=404, detail="foresight threshold not found")
    return threshold


async def _require_deviation_class(session: AsyncSession, term_id: uuid.UUID | None) -> None:
    if term_id is None:
        return
    exists = (
        await session.execute(
            select(OntologyTerm.id).where(
                OntologyTerm.id == term_id, OntologyTerm.category == "deviation_class"
            )
        )
    ).scalar_one_or_none()
    if exists is None:
        raise HTTPException(
            status_code=422, detail=f"deviation_class_term_id: no such deviation_class term {term_id}"
        )


@threshold_router.get("", response_model=list[ForesightThresholdOut])
async def list_thresholds(
    session: Annotated[AsyncSession, Depends(get_session)],
    _admin: Annotated[User, Depends(require_org_administrator)],
) -> list[ForesightThreshold]:
    return list((await session.execute(select(ForesightThreshold))).scalars().all())


@threshold_router.post("", response_model=ForesightThresholdOut, status_code=201)
async def create_threshold(
    body: ForesightThresholdCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    admin: Annotated[User, Depends(require_org_administrator)],
) -> ForesightThreshold:
    await _require_deviation_class(session, body.deviation_class_term_id)
    threshold = ForesightThreshold(
        organisation_id=admin.organisation_id,
        project_id=body.project_id,
        deviation_class_term_id=body.deviation_class_term_id,
        metric=body.metric,
        value=body.value,
    )
    session.add(threshold)
    await session.commit()
    return threshold


@threshold_router.patch("/{threshold_id}", response_model=ForesightThresholdOut)
async def update_threshold(
    threshold_id: uuid.UUID,
    body: ForesightThresholdUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    _admin: Annotated[User, Depends(require_org_administrator)],
) -> ForesightThreshold:
    threshold = await _get_threshold(session, threshold_id)
    threshold.value = body.value
    await session.flush()
    await session.refresh(threshold, attribute_names=["updated_at"])
    await session.commit()
    return threshold


@threshold_router.delete("/{threshold_id}", status_code=204)
async def delete_threshold(
    threshold_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    _admin: Annotated[User, Depends(require_org_administrator)],
) -> None:
    threshold = await _get_threshold(session, threshold_id)
    await session.delete(threshold)
    await session.commit()


@quiet_hours_router.get("", response_model=QuietHoursConfigOut | None)
async def read_quiet_hours(
    project: Annotated[Project, Depends(get_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> QuietHoursConfig | None:
    stmt = select(QuietHoursConfig).where(QuietHoursConfig.project_id == project.id)
    return (await session.execute(stmt)).scalar_one_or_none()


@quiet_hours_router.put("", response_model=QuietHoursConfigOut)
async def set_quiet_hours(
    body: QuietHoursConfigWrite,
    project: Annotated[Project, Depends(_require_write)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> QuietHoursConfig:
    """Upsert — one row per project (QuietHoursConfig's own unique
    constraint), so this is idempotent PUT rather than a POST that could
    violate it on a second call."""
    existing = (
        await session.execute(select(QuietHoursConfig).where(QuietHoursConfig.project_id == project.id))
    ).scalar_one_or_none()
    if existing is None:
        config = QuietHoursConfig(
            project_id=project.id,
            quiet_start_local=body.quiet_start_local,
            quiet_end_local=body.quiet_end_local,
            critical_severity_threshold=body.critical_severity_threshold,
        )
        session.add(config)
    else:
        config = existing
        config.quiet_start_local = body.quiet_start_local
        config.quiet_end_local = body.quiet_end_local
        config.critical_severity_threshold = body.critical_severity_threshold
        await session.flush()
        await session.refresh(config, attribute_names=["updated_at"])
    await session.commit()
    return config
