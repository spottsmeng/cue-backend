import uuid
from datetime import datetime, timezone as dt_timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_actor_id, get_project, require_project_role
from app.core.db import get_session
from app.documents.storage import StorageBackend, get_storage_backend
from app.identity.service import ADMIN_ROLES
from app.models import Project
from app.reports import service as reports_service
from app.reports.models import ReportScheduleConfig
from app.reports.schema import (
    LivingWipReportOut,
    ReportFormatLiteral,
    ReportScheduleConfigCreate,
    ReportScheduleConfigOut,
    ReportScheduleConfigUpdate,
    ReportSnapshotDetailOut,
    ReportSnapshotOut,
)

# FR-RPT-05/06: "Producers can edit... export blocked until verified" — the
# same tier app/identity/service.py's ADMIN_ROLES already gates project
# provisioning and channel attachment with ({"administrator", "producer"}),
# reused here rather than inventing a report-specific role set, per §12.2's
# "Freeze & Export" being a Producer-owned control.
_require_producer = require_project_role(*ADMIN_ROLES)

router = APIRouter(prefix="/projects/{project_id}/report", tags=["reports"])


def _snapshot_out(snapshot, storage: StorageBackend) -> ReportSnapshotOut:
    return ReportSnapshotOut(
        id=snapshot.id,
        project_id=snapshot.project_id,
        format=snapshot.format,
        template_code=snapshot.template_code,
        trigger=snapshot.trigger,
        generated_by=snapshot.generated_by,
        generated_at=snapshot.generated_at,
        download_url=storage.signed_url(snapshot.storage_ref),
    )


@router.get("/current", response_model=LivingWipReportOut)
async def get_current_report(
    project: Annotated[Project, Depends(get_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
) -> LivingWipReportOut:
    """FR-RPT-01: recomputed fresh on every call — no caching this session."""
    return await reports_service.get_current_report(session, project, storage)


@router.post("/export", response_model=ReportSnapshotOut, status_code=201)
async def export_report(
    project: Annotated[Project, Depends(_require_producer)],
    session: Annotated[AsyncSession, Depends(get_session)],
    actor_id: Annotated[uuid.UUID, Depends(get_actor_id)],
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
    format: ReportFormatLiteral,
    template_code: str | None = None,
) -> ReportSnapshotOut:
    """FR-RPT-06/07/08: blocked with 409 while any commitment relevant to
    the budget summary sits at pending_verification; otherwise renders and
    stores a new, immutable snapshot."""
    try:
        snapshot = await reports_service.export_report(
            session,
            project=project,
            storage=storage,
            format=format,
            template_code=template_code,
            actor_id=actor_id,
            trigger="manual",
        )
    except reports_service.ExportBlocked as e:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "export blocked: monetary fields pending verification (FR-RPT-06)",
                "blocking_commitments": [
                    {
                        "commitment_id": str(c.id),
                        "deliverable_en": c.deliverable_en,
                        "reason": "pending_verification",
                    }
                    for c in e.blocking
                ],
            },
        ) from e
    await session.commit()
    return _snapshot_out(snapshot, storage)


@router.get("/snapshots", response_model=list[ReportSnapshotOut])
async def list_snapshots(
    project: Annotated[Project, Depends(get_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
) -> list[ReportSnapshotOut]:
    snapshots = await reports_service.list_snapshots(session, project)
    return [_snapshot_out(s, storage) for s in snapshots]


@router.get("/snapshots/{snapshot_id}", response_model=ReportSnapshotDetailOut)
async def read_snapshot(
    snapshot_id: uuid.UUID,
    project: Annotated[Project, Depends(get_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
) -> ReportSnapshotDetailOut:
    """FR-RPT-08: "a deck presented to a client is reproducible later" —
    `report_json` is exactly what was composed at export time, independent
    of how the ledger has moved on since."""
    snapshot = await reports_service.get_snapshot(session, project, snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="report snapshot not found")
    base = _snapshot_out(snapshot, storage)
    return ReportSnapshotDetailOut(**base.model_dump(), report_json=snapshot.report_json)


# --- FR-RPT-09/10: schedule config surface -------------------------------


async def _get_schedule(
    session: AsyncSession, project: Project, schedule_id: uuid.UUID
) -> ReportScheduleConfig:
    schedule = (
        await session.execute(
            select(ReportScheduleConfig).where(
                ReportScheduleConfig.id == schedule_id, ReportScheduleConfig.project_id == project.id
            )
        )
    ).scalar_one_or_none()
    if schedule is None:
        raise HTTPException(status_code=404, detail="report schedule not found")
    return schedule


@router.get("/schedule", response_model=list[ReportScheduleConfigOut])
async def list_schedules(
    project: Annotated[Project, Depends(get_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[ReportScheduleConfig]:
    schedules = (
        await session.execute(
            select(ReportScheduleConfig).where(ReportScheduleConfig.project_id == project.id)
        )
    ).scalars().all()
    return list(schedules)


@router.post("/schedule", response_model=ReportScheduleConfigOut, status_code=201)
async def create_schedule(
    body: ReportScheduleConfigCreate,
    project: Annotated[Project, Depends(_require_producer)],
    session: Annotated[AsyncSession, Depends(get_session)],
    actor_id: Annotated[uuid.UUID, Depends(get_actor_id)],
) -> ReportScheduleConfig:
    schedule = ReportScheduleConfig(
        project_id=project.id,
        day_of_week=body.day_of_week,
        hour_local=body.hour_local,
        minute_local=body.minute_local,
        format=body.format,
        template_code=body.template_code,
        created_by=actor_id,
    )
    session.add(schedule)
    await session.commit()
    return schedule


@router.patch("/schedule/{schedule_id}", response_model=ReportScheduleConfigOut)
async def update_schedule(
    schedule_id: uuid.UUID,
    body: ReportScheduleConfigUpdate,
    project: Annotated[Project, Depends(_require_producer)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ReportScheduleConfig:
    schedule = await _get_schedule(session, project, schedule_id)
    changes = body.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(schedule, field, value)
    if changes:
        await session.flush()
        # updated_at has a server-side onupdate=func.now() — same
        # MissingGreenlet trap app/api/commitments.py's _refresh_updated_at
        # documents.
        await session.refresh(schedule, attribute_names=["updated_at"])
    await session.commit()
    return schedule


@router.delete("/schedule/{schedule_id}", status_code=204)
async def delete_schedule(
    schedule_id: uuid.UUID,
    project: Annotated[Project, Depends(_require_producer)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    schedule = await _get_schedule(session, project, schedule_id)
    await session.delete(schedule)
    await session.commit()
