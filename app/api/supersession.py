import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_actor_id, get_project, require_project_role
from app.api.schemas import CommitmentSupersessionCandidateOut, CommitmentSupersessionCandidateStatusLiteral
from app.core.db import get_session
from app.identity.service import WRITE_ROLES
from app.ledger.supersession import confirm_supersession_candidate, reject_supersession_candidate
from app.models import CommitmentSupersessionCandidate, Project

# FR-LED-05: the review surface for app/ledger/supersession.py's AI-proposed,
# human-confirmed candidate links — same read tier as every other project-
# scoped GET (any member), same WRITE_ROLES-gated confirm/reject shape
# app/api/deviations.py already established for its own auto_drafted ->
# confirmed review action.
_require_write = require_project_role(*WRITE_ROLES)

router = APIRouter(prefix="/projects/{project_id}/commitments/supersession-candidates", tags=["ledger"])


async def _get_candidate(
    session: AsyncSession, project: Project, candidate_id: uuid.UUID
) -> CommitmentSupersessionCandidate:
    candidate = (
        await session.execute(
            select(CommitmentSupersessionCandidate).where(
                CommitmentSupersessionCandidate.id == candidate_id,
                CommitmentSupersessionCandidate.project_id == project.id,
            )
        )
    ).scalar_one_or_none()
    if candidate is None:
        raise HTTPException(status_code=404, detail="supersession candidate not found")
    return candidate


@router.get("", response_model=list[CommitmentSupersessionCandidateOut])
async def list_supersession_candidates(
    project: Annotated[Project, Depends(get_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
    status: CommitmentSupersessionCandidateStatusLiteral | None = None,
) -> list[CommitmentSupersessionCandidate]:
    """Defaults to every status, not just `pending` — a reviewer may want
    to see what was already confirmed/rejected on this project, the same
    "no server-side default filter hiding history" shape
    `list_deviations`/`list_risks` already give their own status params."""
    stmt = select(CommitmentSupersessionCandidate).where(
        CommitmentSupersessionCandidate.project_id == project.id
    )
    if status is not None:
        stmt = stmt.where(CommitmentSupersessionCandidate.status == status)
    stmt = stmt.order_by(CommitmentSupersessionCandidate.created_at.desc())
    return list((await session.execute(stmt)).scalars().all())


@router.post("/{candidate_id}/confirm", response_model=CommitmentSupersessionCandidateOut)
async def confirm_supersession_candidate_endpoint(
    candidate_id: uuid.UUID,
    project: Annotated[Project, Depends(_require_write)],
    session: Annotated[AsyncSession, Depends(get_session)],
    actor_id: Annotated[uuid.UUID, Depends(get_actor_id)],
) -> CommitmentSupersessionCandidate:
    candidate = await _get_candidate(session, project, candidate_id)
    if candidate.status != "pending":
        raise HTTPException(status_code=409, detail=f"candidate is already {candidate.status}, not pending")
    candidate = await confirm_supersession_candidate(session, candidate=candidate, actor_id=actor_id)
    await session.commit()
    return candidate


@router.post("/{candidate_id}/reject", response_model=CommitmentSupersessionCandidateOut)
async def reject_supersession_candidate_endpoint(
    candidate_id: uuid.UUID,
    project: Annotated[Project, Depends(_require_write)],
    session: Annotated[AsyncSession, Depends(get_session)],
    actor_id: Annotated[uuid.UUID, Depends(get_actor_id)],
) -> CommitmentSupersessionCandidate:
    candidate = await _get_candidate(session, project, candidate_id)
    if candidate.status != "pending":
        raise HTTPException(status_code=409, detail=f"candidate is already {candidate.status}, not pending")
    candidate = await reject_supersession_candidate(session, candidate=candidate, actor_id=actor_id)
    await session.commit()
    return candidate
