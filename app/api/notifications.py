import uuid
from datetime import datetime, timezone as dt_timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_actor_id, get_project
from app.api.schemas import NotificationOut
from app.core.db import get_session
from app.foresight.models import Notification
from app.models import Project

router = APIRouter(prefix="/projects/{project_id}/notifications", tags=["foresight"])


async def _get_notification(
    session: AsyncSession, project: Project, notification_id: uuid.UUID
) -> Notification:
    notification = (
        await session.execute(
            select(Notification).where(
                Notification.id == notification_id, Notification.project_id == project.id
            )
        )
    ).scalar_one_or_none()
    if notification is None:
        raise HTTPException(status_code=404, detail="notification not found")
    return notification


@router.get("", response_model=list[NotificationOut])
async def list_my_notifications(
    project: Annotated[Project, Depends(get_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
    actor_id: Annotated[uuid.UUID, Depends(get_actor_id)],
) -> list[Notification]:
    """Read access only (Depends(get_project), not a write-role gate) —
    every project member can see their own notifications; always scoped to
    the caller's own recipient_id, never another member's inbox."""
    stmt = (
        select(Notification)
        .where(Notification.project_id == project.id, Notification.recipient_id == actor_id)
        .order_by(Notification.created_at.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


@router.post("/{notification_id}/acknowledge", response_model=NotificationOut)
async def acknowledge_notification(
    notification_id: uuid.UUID,
    project: Annotated[Project, Depends(get_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
    actor_id: Annotated[uuid.UUID, Depends(get_actor_id)],
) -> Notification:
    """FR-NTF-05: a recipient acknowledging their own notification — only
    the recipient can acknowledge it, not any project member with write
    access, since this is a personal "I've seen this" action, not a
    project-level mutation."""
    notification = await _get_notification(session, project, notification_id)
    if notification.recipient_id != actor_id:
        raise HTTPException(status_code=403, detail="only the recipient can acknowledge this notification")
    if notification.acknowledged_at is None:
        notification.acknowledged_at = datetime.now(dt_timezone.utc)
        notification.acknowledged_by = actor_id
        await session.flush()
        await session.commit()
    return notification
