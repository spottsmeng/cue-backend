import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_org_id, get_project
from app.api.schemas import ProjectCreate, ProjectOut
from app.core.db import get_session
from app.models import Project, Vertical

router = APIRouter(prefix="/projects", tags=["projects"])


@router.post("", response_model=ProjectOut, status_code=201)
async def create_project(
    body: ProjectCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    org_id: Annotated[uuid.UUID, Depends(get_org_id)],
) -> Project:
    vertical = (
        await session.execute(select(Vertical).where(Vertical.code == body.vertical_code))
    ).scalar_one_or_none()
    if vertical is None:
        raise HTTPException(
            status_code=422, detail=f"unknown vertical_code: {body.vertical_code!r}"
        )

    project = Project(
        organisation_id=org_id,
        vertical_id=vertical.id,
        name=body.name,
        client_name=body.client_name,
        venue=body.venue,
        timezone=body.timezone,
    )
    session.add(project)
    await session.commit()
    # No session.refresh() here: it would issue a fresh SELECT in a new
    # transaction, and app.current_org_id (is_local=true, scoped to the
    # transaction that just committed) is gone by then — RLS would hide the
    # very row just inserted. async_session_factory's expire_on_commit=False
    # means the object's attributes (including server_default created_at/
    # updated_at, populated via Postgres's RETURNING on INSERT) are already
    # usable without one.
    return project


@router.get("", response_model=list[ProjectOut])
async def list_projects(
    session: Annotated[AsyncSession, Depends(get_session)],
    org_id: Annotated[uuid.UUID, Depends(get_org_id)],  # noqa: ARG001 — sets RLS context; scoping itself is RLS's job, not an app-level filter
) -> list[Project]:
    projects = (
        await session.execute(select(Project).order_by(Project.created_at.desc()))
    ).scalars().all()
    return list(projects)


@router.get("/{project_id}", response_model=ProjectOut)
async def read_project(project: Annotated[Project, Depends(get_project)]) -> Project:
    return project
