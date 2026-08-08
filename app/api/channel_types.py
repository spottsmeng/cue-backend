from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.schemas import ChannelTypeOut
from app.core.db import get_session
from app.identity.models import User
from app.models import ChannelType

# Read-only discovery of valid Channel.type / Evidence.channel values, now
# that they're reference-table rows rather than a static Literal a client
# could introspect at compile time (app/api/schemas.py's ChannelTypeOut
# docstring). Gated by plain get_current_user, same tier as
# GET /projects/{id}/channels — not the future tenant-facing Configuration
# UI (no create/edit here). get_current_user sets app.current_org_id, so
# the channel_types RLS policy (organisation_id IS NULL OR = caller's org)
# does the tenant-extension filtering, not this endpoint.
router = APIRouter(prefix="/channel-types", tags=["channel-types"])


@router.get("", response_model=list[ChannelTypeOut])
async def list_channel_types(
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    capability: str | None = None,
) -> list[ChannelType]:
    stmt = select(ChannelType).where(ChannelType.active.is_(True)).order_by(ChannelType.code)
    if capability:
        stmt = stmt.where(ChannelType.capability == capability)
    return list((await session.execute(stmt)).scalars().all())
