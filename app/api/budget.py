import uuid
from datetime import datetime, timezone as dt_timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_actor_id, get_project, require_project_role
from app.api.schemas import BudgetOut, BudgetWrite
from app.core.db import get_session
from app.identity.service import FINANCE_ROLES
from app.models import Budget, Evidence, Project

_require_finance = require_project_role(*FINANCE_ROLES)

router = APIRouter(prefix="/projects/{project_id}/budget", tags=["budget"])


async def _get_current_budget(session: AsyncSession, project_id: uuid.UUID) -> Budget | None:
    return (
        await session.execute(
            select(Budget).where(Budget.project_id == project_id, Budget.is_current.is_(True))
        )
    ).scalar_one_or_none()


def _evidence_for(budget: Budget, actor_id: uuid.UUID) -> Evidence:
    """PRD §4.3: 'an approval message, PO or signed quote where one exists;
    otherwise the approving user's action itself, per the same rule as
    FR-LED-10' — this session has no import/message path for a Budget yet,
    so it's always the latter, same evidence shape
    app/api/commitments.py's create_commitment uses for manual commitments.
    Budget's own evidence-required trigger (migration a895ae03ec5c) checks
    for this row at COMMIT — not written here, per Prompt 5's own note."""
    return Evidence(
        budget_id=budget.id,
        channel="manual",
        sent_at=datetime.now(dt_timezone.utc),
        language="en",
        original_text=f"Budget baseline recorded via the CUE API by user {actor_id} (FR-ADM-11).",
    )


@router.get("", response_model=BudgetOut)
async def read_current_budget(
    project: Annotated[Project, Depends(get_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Budget:
    budget = await _get_current_budget(session, project.id)
    if budget is None:
        raise HTTPException(status_code=404, detail="no budget baseline exists for this project yet")
    return budget


@router.post("", response_model=BudgetOut, status_code=201)
async def create_budget_baseline(
    body: BudgetWrite,
    project: Annotated[Project, Depends(_require_finance)],
    session: Annotated[AsyncSession, Depends(get_session)],
    actor_id: Annotated[uuid.UUID, Depends(get_actor_id)],
) -> Budget:
    """FR-ADM-11: 'capture an approved project budget baseline' — the first
    Budget row for this project. Once a baseline exists, further changes go
    through POST .../revise instead (a new row, never an in-place edit, per
    Budget's own docstring)."""
    existing = await _get_current_budget(session, project.id)
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail="a budget baseline already exists for this project — use POST .../revise",
        )

    budget = Budget(
        project_id=project.id,
        approved_amount=body.approved_amount,
        currency=body.currency,
        approved_by=actor_id,
        approved_at=datetime.now(dt_timezone.utc),
        revision_of=None,
        is_current=True,
    )
    session.add(budget)
    await session.flush()  # need budget.id for the evidence row below

    session.add(_evidence_for(budget, actor_id))
    await session.flush()
    await session.commit()
    return budget


@router.post("/revise", response_model=BudgetOut, status_code=201)
async def revise_budget(
    body: BudgetWrite,
    project: Annotated[Project, Depends(_require_finance)],
    session: Annotated[AsyncSession, Depends(get_session)],
    actor_id: Annotated[uuid.UUID, Depends(get_actor_id)],
) -> Budget:
    """FR-ADM-11's 'full audit trail and revision history on scope-approved
    changes' — always inserts a new Budget row (revision_of set to the prior
    baseline's id), the prior row's is_current flipped false, never an
    in-place UPDATE of approved_amount/currency on the existing row (Budget's
    own docstring)."""
    current = await _get_current_budget(session, project.id)
    if current is None:
        raise HTTPException(
            status_code=404,
            detail="no budget baseline exists for this project yet — use POST to create one",
        )

    revision = Budget(
        project_id=project.id,
        approved_amount=body.approved_amount,
        currency=body.currency,
        approved_by=actor_id,
        approved_at=datetime.now(dt_timezone.utc),
        revision_of=current.id,
        is_current=True,
    )
    session.add(revision)
    await session.flush()  # need revision.id for the evidence row below

    session.add(_evidence_for(revision, actor_id))
    current.is_current = False
    await session.flush()
    await session.commit()
    return revision
