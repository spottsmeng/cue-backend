import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_actor_id, get_project
from app.api.schemas import (
    AffectedNodeOut,
    PropagateCandidateResult,
    PropagateRequest,
    PropagateResponse,
    TwinConstraintOut,
    TwinCurrentOut,
    TwinNodeOut,
)
from app.core.db import get_session
from app.models import Project
from app.twin.audit import record_twin_audit_event
from app.twin.graph import TwinResult
from app.twin.service import compute_current, compute_propagation

router = APIRouter(prefix="/projects/{project_id}/twin", tags=["twin"])


def _to_current_out(result: TwinResult) -> TwinCurrentOut:
    return TwinCurrentOut(
        nodes=[
            TwinNodeOut(
                milestone_id=n.milestone_id,
                earliest=n.earliest,
                latest=n.latest,
                slack_days=n.slack_days,
                is_critical=n.is_critical,
            )
            for n in result.nodes.values()
        ],
        critical_path=result.critical_path,
        binding_constraint=result.binding_constraint,
    )


@router.get("/current", response_model=TwinCurrentOut)
async def twin_current(
    project: Annotated[Project, Depends(get_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TwinCurrentOut:
    """FR-TWN-03. Computed fresh from the current Milestone/Dependency rows
    on every call (app/twin/service.py's compute_current docstring) — no
    cached snapshot, so this and /recompute below are equivalent reads; see
    /recompute for why the endpoint still exists separately."""
    result = await compute_current(session, project.id)
    return _to_current_out(result)


@router.post("/recompute", response_model=TwinCurrentOut)
async def twin_recompute(
    project: Annotated[Project, Depends(get_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
    actor_id: Annotated[uuid.UUID, Depends(get_actor_id)],
) -> TwinCurrentOut:
    """PRD §11.2 lists `recompute` as its own operation distinct from
    `current`. Since there's no cache for either to invalidate, the two are
    computationally identical (app/twin/service.py's compute_current runs
    live off the DB every time) — what actually distinguishes this endpoint
    is that it's an explicit, PM-triggered action, so it leaves an audit
    trail entry behind (a manual "I asked for a fresh look" is worth
    recording; a routine page load is not). No Milestone/Dependency row is
    ever touched by either path."""
    result = await compute_current(session, project.id)
    await record_twin_audit_event(
        session,
        project_id=project.id,
        action="recompute",
        actor_id=actor_id,
        detail={"triggered_by": "manual", "binding_constraint": str(result.binding_constraint) if result.binding_constraint else None},
    )
    await session.commit()
    return _to_current_out(result)


@router.get("/constraint", response_model=TwinConstraintOut)
async def twin_constraint(
    project: Annotated[Project, Depends(get_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TwinConstraintOut:
    """FR-TWN-06, for the *current* (unshifted) graph — see /propagate for
    the binding constraint after a hypothetical shift, which can differ."""
    result = await compute_current(session, project.id)
    node = result.nodes.get(result.binding_constraint) if result.binding_constraint else None
    return TwinConstraintOut(
        binding_constraint=result.binding_constraint,
        slack_days=node.slack_days if node else None,
    )


@router.post("/propagate", response_model=PropagateResponse)
async def twin_propagate(
    body: PropagateRequest,
    project: Annotated[Project, Depends(get_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PropagateResponse:
    """FR-TWN-05/06/07. A pure read — simulates each candidate shift and
    reports its impact; writes nothing to the database (app/twin/graph.py's
    propagate() docstring explains why this one endpoint also serves
    FR-TWN-07's "candidate recovery options," rather than a second one)."""
    results: list[PropagateCandidateResult] = []
    for candidate in body.candidates:
        try:
            propagation = await compute_propagation(
                session, project.id, candidate.milestone_id, candidate.new_date
            )
        except KeyError as e:
            raise HTTPException(
                status_code=422, detail=f"milestone_id: no such milestone {candidate.milestone_id}"
            ) from e
        results.append(
            PropagateCandidateResult(
                milestone_id=propagation.shifted_milestone_id,
                new_date=propagation.new_date,
                affected=[
                    AffectedNodeOut(
                        milestone_id=a.milestone_id,
                        previous_earliest=a.previous_earliest,
                        new_earliest=a.new_earliest,
                        consumed_slack_days=a.consumed_slack_days,
                        propagation_stopped=a.propagation_stopped,
                    )
                    for a in propagation.affected
                ],
                binding_constraint_after=propagation.binding_constraint_after,
            )
        )
    return PropagateResponse(results=results)
