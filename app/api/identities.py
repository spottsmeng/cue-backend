from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_org_administrator
from app.api.schemas import ChannelIdentityOut, ChannelIdentityOverrideRequest
from app.capture.identity import set_manual_identity_override
from app.core.db import get_session
from app.identity.models import User
from app.models import ChannelIdentity, Party

# FR-NRM-03's "manual override" surface — org-wide, same
# require_org_administrator shape app/api/consent.py already uses, since a
# ChannelIdentity has no project_id of its own (a resolved identity is an
# organisation-level fact, a person can appear on more than one project's
# channels). channel_identities itself has no RLS policy of its own — same
# pre-existing gap Party has (app/api/consent.py's own note) — so every
# query here filters through parties.organisation_id explicitly, same
# "verified in application code, not trusted to RLS" posture that call site
# already established.
router = APIRouter(prefix="/admin/channel-identities", tags=["identity"])


@router.get("", response_model=list[ChannelIdentityOut])
async def list_channel_identities(
    session: Annotated[AsyncSession, Depends(get_session)],
    admin: Annotated[User, Depends(require_org_administrator)],
    max_confidence: float | None = None,
    manually_verified: bool | None = None,
) -> list[ChannelIdentity]:
    """`max_confidence` is what an Administrator reviewing low-confidence
    auto-resolutions actually needs (e.g. `?max_confidence=0.7` to surface
    every display-name-heuristic match app/capture/identity.py's
    resolve_identity produced) — not full CRUD, since the only write this
    domain needs is the override below."""
    stmt = (
        select(ChannelIdentity)
        .join(Party, Party.id == ChannelIdentity.party_id)
        .where(Party.organisation_id == admin.organisation_id)
    )
    if max_confidence is not None:
        stmt = stmt.where(ChannelIdentity.confidence <= max_confidence)
    if manually_verified is not None:
        stmt = stmt.where(ChannelIdentity.manually_verified.is_(manually_verified))
    stmt = stmt.order_by(ChannelIdentity.confidence.asc())
    return list((await session.execute(stmt)).scalars().all())


@router.post("/override", response_model=ChannelIdentityOut)
async def override_channel_identity(
    body: ChannelIdentityOverrideRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    admin: Annotated[User, Depends(require_org_administrator)],
) -> ChannelIdentity:
    party = (
        await session.execute(
            select(Party.id).where(
                Party.id == body.party_id, Party.organisation_id == admin.organisation_id
            )
        )
    ).scalar_one_or_none()
    if party is None:
        raise HTTPException(status_code=422, detail=f"party_id: no such party {body.party_id}")

    identity = await set_manual_identity_override(
        session, channel_type=body.channel_type, external_id=body.external_id, party_id=body.party_id
    )
    await session.commit()
    return identity
