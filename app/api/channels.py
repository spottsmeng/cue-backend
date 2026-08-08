import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_project, require_project_role
from app.api.schemas import ChannelCreate, ChannelHealthSignal, ChannelOut
from app.core.db import get_session
from app.identity.service import ADMIN_ROLES
from app.models import Channel, ChannelType, Project

# FR-ADM-06 names "attach channels" as part of project provisioning, the same
# admin-tier surface app/api/projects.py's add_member already gates with
# ADMIN_ROLES — health/reconnect are grouped under the same FR-ADM-09
# governance requirement, so this router gates every write the same way.
# There is no service-account/agent identity yet for a real capture agent to
# call the health endpoint as (out of scope this session, see Prompt 5) —
# when one exists, it will need its own auth path, not a project membership
# role; this is the placeholder until then.
_require_admin = require_project_role(*ADMIN_ROLES)

router = APIRouter(prefix="/projects/{project_id}/channels", tags=["channels"])


async def _refresh_updated_at(session: AsyncSession, channel: Channel) -> None:
    """`updated_at` has a server-side `onupdate=func.now()` — same
    MissingGreenlet trap app/api/commitments.py's _refresh_updated_at
    documents: after an UPDATE flush, SQLAlchemy expires it rather than
    fetching it eagerly, and a bare attribute access later would trigger a
    lazy-load outside the async greenlet bridge."""
    await session.refresh(channel, attribute_names=["updated_at"])


async def _resolve_channel_type(session: AsyncSession, code: str, *, require_capability: bool) -> str:
    """channel_types.code replaced ChannelTypeLiteral's static closed set
    (app/api/schemas.py) — validity is now a DB lookup, not a Python type,
    since the whole point of the reference-data table is that a new code is
    an insert, not a code change. `require_capability=True` is the FK-free
    equivalent of the old Literal's own "minus manual" carve-out: a Channel
    resource can never be "manual" (that value exists only for
    Evidence.channel's non-integration case, FR-LED-10)."""
    stmt = select(ChannelType.code).where(ChannelType.code == code, ChannelType.active.is_(True))
    if require_capability:
        stmt = stmt.where(ChannelType.capability.is_not(None))
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=422, detail=f"unknown or non-attachable channel type: {code!r}")
    return row


async def _get_channel(session: AsyncSession, project: Project, channel_id: uuid.UUID) -> Channel:
    channel = (
        await session.execute(
            select(Channel).where(Channel.id == channel_id, Channel.project_id == project.id)
        )
    ).scalar_one_or_none()
    if channel is None:
        raise HTTPException(status_code=404, detail="channel not found")
    return channel


@router.get("", response_model=list[ChannelOut])
async def list_channels(
    project: Annotated[Project, Depends(get_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[Channel]:
    channels = (
        await session.execute(select(Channel).where(Channel.project_id == project.id))
    ).scalars().all()
    return list(channels)


@router.post("", response_model=ChannelOut, status_code=201)
async def attach_channel(
    body: ChannelCreate,
    project: Annotated[Project, Depends(_require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Channel:
    """FR-ADM-06: 'attach channels' — the half of project provisioning the
    RBAC/delegation session left for this one (create/assign members was
    already built)."""
    channel_type = await _resolve_channel_type(session, body.type, require_capability=True)
    channel = Channel(project_id=project.id, type=channel_type, external_ref=body.external_ref, healthy=True)
    session.add(channel)
    await session.commit()
    return channel


@router.delete("/{channel_id}", status_code=204)
async def detach_channel(
    channel_id: uuid.UUID,
    project: Annotated[Project, Depends(_require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    channel = await _get_channel(session, project, channel_id)
    await session.delete(channel)
    await session.commit()


@router.post("/{channel_id}/health", response_model=ChannelOut)
async def report_channel_health(
    channel_id: uuid.UUID,
    body: ChannelHealthSignal,
    project: Annotated[Project, Depends(_require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Channel:
    """FR-ADM-09: the receiving end of a capture-health signal — nothing
    calls this yet (a later milestone's real capture agents will), but the
    endpoint exists now so that milestone is wiring, not building, this
    surface. `body.detail` is accepted and returned to the caller but not
    persisted — Channel has no column for it, and no consumer of channel
    health history exists yet (out of scope this session)."""
    channel = await _get_channel(session, project, channel_id)
    channel.healthy = body.healthy
    await session.flush()
    await _refresh_updated_at(session, channel)
    await session.commit()
    return channel


@router.post("/{channel_id}/reconnect", response_model=ChannelOut)
async def reconnect_channel(
    channel_id: uuid.UUID,
    project: Annotated[Project, Depends(_require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Channel:
    """FR-ADM-09's reconnection workflow — an explicit admin action marking
    a degraded channel healthy again, distinct from a health signal simply
    reporting `healthy: true` on its own."""
    channel = await _get_channel(session, project, channel_id)
    channel.healthy = True
    await session.flush()
    await _refresh_updated_at(session, channel)
    await session.commit()
    return channel
