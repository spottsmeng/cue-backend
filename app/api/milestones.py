import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_actor_id, get_project, require_project_role
from app.api.schemas import (
    DependencyCreate,
    DependencyOut,
    DependencyUpdate,
    MilestoneCreate,
    MilestoneOut,
    MilestoneUpdate,
)
from app.core.db import get_session
from app.identity.service import WRITE_ROLES
from app.models import Milestone, Project
from app.twin.audit import record_twin_audit_event
from app.twin.graph import CycleError, DependencyEdge, compute_twin
from app.twin.models import Dependency
from app.twin.service import build_graph, get_milestone_type_term

_require_write = require_project_role(*WRITE_ROLES)

router = APIRouter(prefix="/projects/{project_id}/milestones", tags=["milestones"])


def _jsonable(value: object) -> object:
    return value.isoformat() if isinstance(value, datetime) else value


async def _get_milestone(session: AsyncSession, project: Project, milestone_id: uuid.UUID) -> Milestone:
    milestone = (
        await session.execute(
            select(Milestone).where(Milestone.id == milestone_id, Milestone.project_id == project.id)
        )
    ).scalar_one_or_none()
    if milestone is None:
        raise HTTPException(status_code=404, detail="milestone not found")
    return milestone


async def _get_dependency(session: AsyncSession, project: Project, dependency_id: uuid.UUID) -> Dependency:
    dependency = (
        await session.execute(
            select(Dependency).where(
                Dependency.id == dependency_id, Dependency.project_id == project.id
            )
        )
    ).scalar_one_or_none()
    if dependency is None:
        raise HTTPException(status_code=404, detail="dependency not found")
    return dependency


@router.get("", response_model=list[MilestoneOut])
async def list_milestones(
    project: Annotated[Project, Depends(get_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[Milestone]:
    milestones = (
        await session.execute(
            select(Milestone)
            .where(Milestone.project_id == project.id)
            .order_by(Milestone.planned_at.asc().nulls_last())
        )
    ).scalars().all()
    return list(milestones)


@router.post("", response_model=MilestoneOut, status_code=201)
async def create_milestone(
    body: MilestoneCreate,
    project: Annotated[Project, Depends(_require_write)],
    session: Annotated[AsyncSession, Depends(get_session)],
    actor_id: Annotated[uuid.UUID, Depends(get_actor_id)],
) -> Milestone:
    """FR-TWN-01/10: a milestone the seeded archetype didn't anticipate —
    override isn't limited to editing what's already there."""
    try:
        term = await get_milestone_type_term(session, project, body.type_code)
    except LookupError as e:
        raise HTTPException(status_code=422, detail=f"type_code: {e}") from e

    milestone = Milestone(
        project_id=project.id,
        type_term_id=term.id,
        name=body.name,
        planned_at=body.planned_at,
        actual_at=body.actual_at,
        is_fixed=body.is_fixed,
    )
    session.add(milestone)
    await session.flush()

    await record_twin_audit_event(
        session,
        project_id=project.id,
        action="milestone_created",
        actor_id=actor_id,
        milestone_id=milestone.id,
        detail={"type_code": body.type_code, "name": body.name},
    )
    await session.commit()
    return milestone


# --- dependencies, registered ahead of /{milestone_id} so the literal path
# segment wins the route match instead of being parsed as a milestone id. ---


@router.get("/dependencies", response_model=list[DependencyOut])
async def list_dependencies(
    project: Annotated[Project, Depends(get_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[Dependency]:
    dependencies = (
        await session.execute(select(Dependency).where(Dependency.project_id == project.id))
    ).scalars().all()
    return list(dependencies)


@router.post("/dependencies", response_model=DependencyOut, status_code=201)
async def create_dependency(
    body: DependencyCreate,
    project: Annotated[Project, Depends(_require_write)],
    session: Annotated[AsyncSession, Depends(get_session)],
    actor_id: Annotated[uuid.UUID, Depends(get_actor_id)],
) -> Dependency:
    """FR-TWN-01/10: an edge the seeded archetype didn't have — e.g. wiring
    a newly added milestone into the graph. Checked for cycles before
    insert (FR-TWN-01 requires a DAG) rather than left for the next
    /twin/current call to discover as a 500."""
    await _get_milestone(session, project, body.upstream_milestone_id)
    await _get_milestone(session, project, body.downstream_milestone_id)

    nodes, edges = await build_graph(session, project.id)
    hypothetical = edges + [
        DependencyEdge(
            upstream_id=body.upstream_milestone_id,
            downstream_id=body.downstream_milestone_id,
            lag_days=body.lag_days,
        )
    ]
    try:
        compute_twin(nodes, hypothetical)
    except CycleError as e:
        raise HTTPException(
            status_code=422, detail="this dependency would create a cycle (FR-TWN-01)"
        ) from e

    dependency = Dependency(
        project_id=project.id,
        upstream_milestone_id=body.upstream_milestone_id,
        downstream_milestone_id=body.downstream_milestone_id,
        lag_days=body.lag_days,
    )
    session.add(dependency)
    try:
        await session.flush()
    except DBAPIError as e:
        await session.rollback()
        raise HTTPException(status_code=409, detail="this dependency edge already exists") from e

    await record_twin_audit_event(
        session,
        project_id=project.id,
        action="dependency_created",
        actor_id=actor_id,
        dependency_id=dependency.id,
        detail={
            "upstream_milestone_id": str(body.upstream_milestone_id),
            "downstream_milestone_id": str(body.downstream_milestone_id),
            "lag_days": body.lag_days,
        },
    )
    await session.commit()
    return dependency


@router.patch("/dependencies/{dependency_id}", response_model=DependencyOut)
async def update_dependency(
    dependency_id: uuid.UUID,
    body: DependencyUpdate,
    project: Annotated[Project, Depends(_require_write)],
    session: Annotated[AsyncSession, Depends(get_session)],
    actor_id: Annotated[uuid.UUID, Depends(get_actor_id)],
) -> Dependency:
    """FR-TWN-10: PM override of a dependency's duration."""
    dependency = await _get_dependency(session, project, dependency_id)
    before = dependency.lag_days
    dependency.lag_days = body.lag_days
    await session.flush()

    await record_twin_audit_event(
        session,
        project_id=project.id,
        action="dependency_override",
        actor_id=actor_id,
        dependency_id=dependency.id,
        detail={"changes": {"lag_days": {"before": before, "after": body.lag_days}}},
    )
    await session.commit()
    return dependency


@router.delete("/dependencies/{dependency_id}", status_code=204)
async def delete_dependency(
    dependency_id: uuid.UUID,
    project: Annotated[Project, Depends(_require_write)],
    session: Annotated[AsyncSession, Depends(get_session)],
    actor_id: Annotated[uuid.UUID, Depends(get_actor_id)],
) -> None:
    """FR-TWN-01/10: removing an edge the seeded archetype had that doesn't
    apply to this project. No cycle risk on removal — only ever loosens the
    graph."""
    dependency = await _get_dependency(session, project, dependency_id)

    await record_twin_audit_event(
        session,
        project_id=project.id,
        action="dependency_deleted",
        actor_id=actor_id,
        dependency_id=dependency.id,
        detail={
            "upstream_milestone_id": str(dependency.upstream_milestone_id),
            "downstream_milestone_id": str(dependency.downstream_milestone_id),
            "lag_days": dependency.lag_days,
        },
    )
    await session.flush()
    await session.delete(dependency)
    await session.commit()


@router.get("/{milestone_id}", response_model=MilestoneOut)
async def read_milestone(
    milestone_id: uuid.UUID,
    project: Annotated[Project, Depends(get_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Milestone:
    return await _get_milestone(session, project, milestone_id)


@router.patch("/{milestone_id}", response_model=MilestoneOut)
async def update_milestone(
    milestone_id: uuid.UUID,
    body: MilestoneUpdate,
    project: Annotated[Project, Depends(_require_write)],
    session: Annotated[AsyncSession, Depends(get_session)],
    actor_id: Annotated[uuid.UUID, Depends(get_actor_id)],
) -> Milestone:
    """FR-TWN-10: a PM override of a milestone's date, fixed-ness or name —
    recorded as an audited change (app/twin/audit.py), never a silent
    overwrite."""
    milestone = await _get_milestone(session, project, milestone_id)

    changes: dict[str, dict[str, object]] = {}
    for field, new_value in body.model_dump(exclude_unset=True).items():
        old_value = getattr(milestone, field)
        if new_value != old_value:
            changes[field] = {"before": _jsonable(old_value), "after": _jsonable(new_value)}
            setattr(milestone, field, new_value)

    if not changes:
        return milestone
    await session.flush()
    # updated_at has a server-side onupdate=func.now() — same MissingGreenlet
    # trap commitments.py's _refresh_updated_at documents, applies here too.
    await session.refresh(milestone, attribute_names=["updated_at"])

    await record_twin_audit_event(
        session,
        project_id=project.id,
        action="milestone_override",
        actor_id=actor_id,
        milestone_id=milestone.id,
        detail={"changes": changes},
    )
    await session.commit()
    return milestone


@router.delete("/{milestone_id}", status_code=204)
async def delete_milestone(
    milestone_id: uuid.UUID,
    project: Annotated[Project, Depends(_require_write)],
    session: Annotated[AsyncSession, Depends(get_session)],
    actor_id: Annotated[uuid.UUID, Depends(get_actor_id)],
) -> None:
    """FR-TWN-01/10: removing a milestone the seeded archetype had that
    doesn't apply to this project. Refuses (409) rather than silently
    cascading if a Dependency edge or Deliverable still references it —
    deleting those first is a deliberate, separate step, not an implicit
    side effect of this one."""
    milestone = await _get_milestone(session, project, milestone_id)

    await record_twin_audit_event(
        session,
        project_id=project.id,
        action="milestone_deleted",
        actor_id=actor_id,
        milestone_id=milestone.id,
        detail={
            "name": milestone.name,
            "type_term_id": str(milestone.type_term_id),
            "planned_at": _jsonable(milestone.planned_at),
        },
    )
    await session.flush()  # audit row committed with a real FK before the delete below

    await session.delete(milestone)
    try:
        await session.commit()
    except DBAPIError as e:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="milestone still referenced by a dependency or deliverable — remove those first",
        ) from e
