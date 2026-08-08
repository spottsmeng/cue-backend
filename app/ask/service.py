"""Thin orchestration over app/ask/answer.py, summarise.py and brief.py —
app/api/ask.py is the HTTP layer on top of this module, same split
app/reports/service.py draws over app/reports/composer.py for its domain.
"""

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.ask import answer as answer_module
from app.ask import brief as brief_module
from app.ask import summarise as summarise_module
from app.ask.embeddings import EmbeddingClient, get_embedding_client
from app.ask.schema import AskAnswerOut, AskSummaryOut, SuccessorBriefOut
from app.llm.client import ModelClient
from app.llm.factory import get_client
from app.models import Project


async def ask_query(
    session: AsyncSession,
    *,
    project: Project,
    user_id: uuid.UUID,
    question: str,
    conversation_id: uuid.UUID | None,
    embedding_client: EmbeddingClient | None = None,
    reasoning_client: ModelClient | None = None,
) -> AskAnswerOut:
    return await answer_module.answer_query(
        session,
        project=project,
        user_id=user_id,
        question=question,
        conversation_id=conversation_id,
        embedding_client=embedding_client or get_embedding_client(),
        reasoning_client=reasoning_client or get_client("reasoning"),
    )


async def ask_summarise(
    session: AsyncSession,
    *,
    project: Project,
    variant: str,
    period_start: datetime | None,
    period_end: datetime | None,
) -> AskSummaryOut:
    if variant == "project_status":
        return AskSummaryOut(
            variant=variant, project_status=await summarise_module.compose_project_status(session, project)
        )
    if variant == "vendor_status":
        return AskSummaryOut(
            variant=variant, vendor_status=await summarise_module.compose_vendor_status(session, project)
        )
    if variant == "period_digest":
        if period_start is None or period_end is None:
            raise ValueError("the period_digest variant requires period_start and period_end")
        return AskSummaryOut(
            variant=variant,
            period_digest=await summarise_module.compose_period_digest(session, project, period_start, period_end),
        )
    if variant == "decision_history":
        return AskSummaryOut(
            variant=variant, decision_history=await summarise_module.compose_decision_history(session, project)
        )
    if variant == "outstanding_actions":
        return AskSummaryOut(
            variant=variant,
            outstanding_actions=await summarise_module.compose_outstanding_actions(session, project),
        )
    raise ValueError(f"unknown summarise variant: {variant!r}")


async def ask_successor_brief(session: AsyncSession, *, project: Project) -> SuccessorBriefOut:
    return await brief_module.compose_successor_brief(session, project)


__all__ = ["ask_query", "ask_summarise", "ask_successor_brief"]
