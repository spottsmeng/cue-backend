import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_actor_id, get_project, require_project_role
from app.core.db import get_session
from app.identity.service import ADMIN_ROLES, WRITE_ROLES
from app.models import Commitment, Project
from app.writeback.audit import record_writeback_audit_event
from app.writeback.compose import ComposeError
from app.writeback.models import OutboundMessage
from app.writeback.rate_limit import RateCeilingExceeded
from app.writeback.schema import (
    OutboundMessageOut,
    OutboundMessageStatusLiteral,
    WritebackConfigOut,
    WritebackConfigUpdate,
    WritebackDraftRequest,
    WritebackDraftUpdate,
)
from app.writeback.service import (
    InvalidWritebackTransition,
    WritebackTargetUnresolved,
    authorise_writeback,
    draft_writeback,
    edit_writeback_draft,
    send_writeback,
)

_require_write = require_project_role(*WRITE_ROLES)
_require_admin = require_project_role(*ADMIN_ROLES)

router = APIRouter(prefix="/projects/{project_id}/writeback", tags=["writeback"])


async def _get_commitment(
    session: AsyncSession, project: Project, commitment_id: uuid.UUID
) -> Commitment:
    commitment = (
        await session.execute(
            select(Commitment).where(
                Commitment.id == commitment_id, Commitment.project_id == project.id
            )
        )
    ).scalar_one_or_none()
    if commitment is None:
        raise HTTPException(status_code=404, detail="commitment not found")
    return commitment


async def _get_outbound(
    session: AsyncSession, project: Project, outbound_id: uuid.UUID
) -> OutboundMessage:
    outbound = (
        await session.execute(
            select(OutboundMessage).where(
                OutboundMessage.id == outbound_id, OutboundMessage.project_id == project.id
            )
        )
    ).scalar_one_or_none()
    if outbound is None:
        raise HTTPException(status_code=404, detail="outbound message not found")
    return outbound


@router.get("", response_model=list[OutboundMessageOut])
async def list_writeback_history(
    project: Annotated[Project, Depends(get_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
    commitment_id: uuid.UUID | None = None,
    channel_id: uuid.UUID | None = None,
    status: OutboundMessageStatusLiteral | None = None,
) -> list[OutboundMessage]:
    """PRD §11.2's "history" operation."""
    stmt = select(OutboundMessage).where(OutboundMessage.project_id == project.id)
    if commitment_id is not None:
        stmt = stmt.where(OutboundMessage.commitment_id == commitment_id)
    if channel_id is not None:
        stmt = stmt.where(OutboundMessage.channel_id == channel_id)
    if status is not None:
        stmt = stmt.where(OutboundMessage.status == status)
    stmt = stmt.order_by(OutboundMessage.created_at.desc())
    return list((await session.execute(stmt)).scalars().all())


@router.get("/{outbound_id}", response_model=OutboundMessageOut)
async def read_writeback(
    outbound_id: uuid.UUID,
    project: Annotated[Project, Depends(get_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> OutboundMessage:
    return await _get_outbound(session, project, outbound_id)


@router.get("/config", response_model=WritebackConfigOut)
async def read_writeback_config(
    project: Annotated[Project, Depends(get_project)],
) -> WritebackConfigOut:
    return WritebackConfigOut(project_id=project.id, daily_ceiling=project.writeback_daily_ceiling)


@router.patch("/config", response_model=WritebackConfigOut)
async def update_writeback_config(
    body: WritebackConfigUpdate,
    project: Annotated[Project, Depends(_require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    actor_id: Annotated[uuid.UUID, Depends(get_actor_id)],
) -> WritebackConfigOut:
    """FR-WBK-04's "no code path ... can exceed it without an explicit,
    audited configuration change to the ceiling itself" — ADMIN_ROLES-gated
    (the same tier channel attach/detach and membership provisioning use,
    per app/api/deps.py's own precedent), and every change is logged to this
    domain's own append-only WritebackAuditLog, since a ceiling change has
    no commitment to hang the shared AuditLog off of."""
    if body.daily_ceiling < 1:
        raise HTTPException(status_code=422, detail="daily_ceiling must be at least 1")

    before = project.writeback_daily_ceiling
    project.writeback_daily_ceiling = body.daily_ceiling
    await session.flush()
    await session.refresh(project, attribute_names=["updated_at"])

    await record_writeback_audit_event(
        session,
        project_id=project.id,
        action="config_updated",
        actor_id=actor_id,
        detail={"before": before, "after": body.daily_ceiling},
    )
    await session.commit()
    return WritebackConfigOut(project_id=project.id, daily_ceiling=project.writeback_daily_ceiling)


@router.post("/draft", response_model=OutboundMessageOut, status_code=201)
async def create_writeback_draft(
    body: WritebackDraftRequest,
    project: Annotated[Project, Depends(_require_write)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> OutboundMessage:
    """FR-WBK-01/02/03. Never sends anything by itself — see the class
    docstring on OutboundMessage for why draft/authorise/send are three
    structurally separate calls."""
    commitment = await _get_commitment(session, project, body.commitment_id)
    try:
        outbound = await draft_writeback(session, project=project, commitment=commitment)
    except WritebackTargetUnresolved as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except ComposeError as e:
        raise HTTPException(status_code=502, detail=f"draft composition failed: {e}") from e
    await session.commit()
    return outbound


@router.patch("/{outbound_id}", response_model=OutboundMessageOut)
async def edit_writeback_draft_endpoint(
    outbound_id: uuid.UUID,
    body: WritebackDraftUpdate,
    project: Annotated[Project, Depends(_require_write)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> OutboundMessage:
    """FR-WBK-05's "review/edit before authorisation", made real in the UI
    (Prompt F1's own instruction) — the edit half; draft/authorise/send
    cover compose/approve/deliver."""
    outbound = await _get_outbound(session, project, outbound_id)
    try:
        outbound = await edit_writeback_draft(session, outbound=outbound, draft_text=body.draft_text)
    except InvalidWritebackTransition as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    await session.commit()
    return outbound


@router.post("/{outbound_id}/authorise", response_model=OutboundMessageOut)
async def authorise_writeback_draft(
    outbound_id: uuid.UUID,
    project: Annotated[Project, Depends(_require_write)],
    session: Annotated[AsyncSession, Depends(get_session)],
    actor_id: Annotated[uuid.UUID, Depends(get_actor_id)],
) -> OutboundMessage:
    """FR-WBK-05's required, separate human step — the only call that ever
    sets authorised_by/authorised_at."""
    outbound = await _get_outbound(session, project, outbound_id)
    try:
        outbound = await authorise_writeback(session, outbound=outbound, actor_id=actor_id)
    except InvalidWritebackTransition as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    await session.commit()
    return outbound


@router.post("/{outbound_id}/send", response_model=OutboundMessageOut)
async def send_writeback_draft(
    outbound_id: uuid.UUID,
    project: Annotated[Project, Depends(_require_write)],
    session: Annotated[AsyncSession, Depends(get_session)],
    actor_id: Annotated[uuid.UUID, Depends(get_actor_id)],
) -> OutboundMessage:
    """FR-WBK-04/05/08: only callable on an already-authorised draft
    (409 otherwise); rejects once the group's rate ceiling for today is
    already met (429); logs the send to the shared, commitment-scoped audit
    trail via app/ledger/audit.py's record_audit_event."""
    outbound = await _get_outbound(session, project, outbound_id)
    try:
        outbound = await send_writeback(session, project=project, outbound=outbound, actor_id=actor_id)
    except InvalidWritebackTransition as e:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(e)) from e
    except RateCeilingExceeded as e:
        await session.rollback()
        raise HTTPException(status_code=429, detail=str(e)) from e
    except NotImplementedError as e:
        await session.rollback()
        raise HTTPException(
            status_code=422, detail=f"this channel does not support outbound messages: {e}"
        ) from e
    await session.commit()
    return outbound
