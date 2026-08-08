import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_actor_id, get_project
from app.ask import service as ask_service
from app.ask.schema import AskAnswerOut, AskQueryRequest, AskSummaryOut, AskSummaryRequest, SuccessorBriefOut
from app.core.db import get_session
from app.models import Project

# PRD §11.2: `/projects/{id}/ask` — query · summarise · successor-brief.
# §12.5: "Ask (all internal users)" — no role beyond plain project
# membership is required for any of these (contrast with reports.py's
# _require_producer for export), same "every retrieval query routed through
# the exact same RLS + membership-scoping every other endpoint uses" posture
# Prompt 9 itself specifies for FR-ASK-03.
router = APIRouter(prefix="/projects/{project_id}/ask", tags=["ask"])


@router.post("/query", response_model=AskAnswerOut)
async def query(
    body: AskQueryRequest,
    project: Annotated[Project, Depends(get_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
    actor_id: Annotated[uuid.UUID, Depends(get_actor_id)],
) -> AskAnswerOut:
    """FR-ASK-01/02/03/08: a natural-language question in, an answer with
    real citations out, or the typed 'no source' variant."""
    try:
        result = await ask_service.ask_query(
            session,
            project=project,
            user_id=actor_id,
            question=body.question,
            conversation_id=body.conversation_id,
        )
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    await session.commit()
    return result


@router.post("/summarise", response_model=AskSummaryOut)
async def summarise(
    body: AskSummaryRequest,
    project: Annotated[Project, Depends(get_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AskSummaryOut:
    """FR-ASK-04/05: project status / vendor status / period digest /
    decision history / outstanding actions — grounded summaries, not
    GET /commitments's raw filterable list."""
    try:
        return await ask_service.ask_summarise(
            session,
            project=project,
            variant=body.variant,
            period_start=body.period_start,
            period_end=body.period_end,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.post("/successor-brief", response_model=SuccessorBriefOut)
async def successor_brief(
    project: Annotated[Project, Depends(get_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SuccessorBriefOut:
    """FR-ASK-07: a full handover context pack in one action."""
    return await ask_service.ask_successor_brief(session, project=project)
