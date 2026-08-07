import secrets
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_actor_id, get_project, require_project_role
from app.api.schemas import WebhookSubscriptionCreate, WebhookSubscriptionCreated, WebhookSubscriptionOut
from app.core.db import get_session
from app.foresight.models import WebhookSubscription
from app.identity.service import WRITE_ROLES
from app.models import Project

# Item 8: "/webhooks subscription endpoint + event dispatch for commitment/
# risk/deviation events" — write-role-gated, same tier every other
# project-mutating resource in this codebase uses (not ADMIN_ROLES: a PM
# wiring their own team's Slack/Teams relay into CUE is an ordinary project
# configuration action, not an administrative one).

_require_write = require_project_role(*WRITE_ROLES)

router = APIRouter(prefix="/projects/{project_id}/webhooks", tags=["foresight"])


async def _get_subscription(
    session: AsyncSession, project: Project, subscription_id: uuid.UUID
) -> WebhookSubscription:
    subscription = (
        await session.execute(
            select(WebhookSubscription).where(
                WebhookSubscription.id == subscription_id, WebhookSubscription.project_id == project.id
            )
        )
    ).scalar_one_or_none()
    if subscription is None:
        raise HTTPException(status_code=404, detail="webhook subscription not found")
    return subscription


@router.get("", response_model=list[WebhookSubscriptionOut])
async def list_webhooks(
    project: Annotated[Project, Depends(get_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[WebhookSubscription]:
    stmt = select(WebhookSubscription).where(WebhookSubscription.project_id == project.id)
    return list((await session.execute(stmt)).scalars().all())


@router.post("", response_model=WebhookSubscriptionCreated, status_code=201)
async def create_webhook(
    body: WebhookSubscriptionCreate,
    project: Annotated[Project, Depends(_require_write)],
    session: Annotated[AsyncSession, Depends(get_session)],
    actor_id: Annotated[uuid.UUID, Depends(get_actor_id)],
) -> WebhookSubscription:
    """Returns the signing secret exactly once — see
    WebhookSubscriptionOut's own docstring."""
    subscription = WebhookSubscription(
        project_id=project.id,
        url=body.url,
        event_types=body.event_types,
        secret=secrets.token_hex(32),
        created_by=actor_id,
    )
    session.add(subscription)
    await session.commit()
    return subscription


@router.delete("/{subscription_id}", status_code=204)
async def delete_webhook(
    subscription_id: uuid.UUID,
    project: Annotated[Project, Depends(_require_write)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    subscription = await _get_subscription(session, project, subscription_id)
    await session.delete(subscription)
    await session.commit()
