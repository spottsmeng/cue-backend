"""GET/PATCH /users/me — F9's own per-user preference surface (NFR-ACC-03).

Frontend gap-closure, same "close the gap you find, document it" pattern as
the post-M10 additions in backend/PROGRESS.md: F9 needs high-contrast mode
"persisted per-user, not just per-session" (its own prompt's wording,
deliberately distinct from the theme toggle's device-local localStorage
design — frontend/lib/store/ui-store.ts's own comment: "a device
preference, not project data... nor should there be" a per-user row here).
There was no per-user settings surface of any kind before this.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.schemas import UserMeOut, UserPreferencesUpdate
from app.core.db import get_session
from app.identity.models import User

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserMeOut)
async def read_me(user: Annotated[User, Depends(get_current_user)]) -> User:
    return user


@router.patch("/me", response_model=UserMeOut)
async def update_me(
    body: UserPreferencesUpdate,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> User:
    user.high_contrast = body.high_contrast
    # flush + refresh BEFORE commit, not after — a real bug this session's
    # own keyboard-only e2e pass caught live (a 500, surfaced to the browser
    # as an opaque CORS failure since a 500 response carries no CORS headers
    # here, masking the real error until the server log was read directly).
    # `get_session`'s own docstring: `app.current_org_id` is set
    # `is_local=true`, scoped to the request's transaction — commit() ends
    # that transaction, so a refresh() called *after* commit runs its SELECT
    # with no RLS context and finds zero rows ("Could not refresh instance").
    # app/api/milestones.py's update_milestone already establishes the
    # correct order (flush, refresh, then commit) for exactly this reason;
    # this endpoint just hadn't followed it.
    await session.flush()
    await session.refresh(user)
    await session.commit()
    return user
