"""Minimal, explicitly-temporary per-request auth/org-context.

THIS IS NOT REAL AUTH. No Entra ID, no session tokens, no RBAC roles — just
two plain headers a caller sets by hand. It exists only so RLS-protected
endpoints have *something* to set `app.current_org_id` from before the real
identity/RBAC module gets built (PRD §6.14, FR-ADM-01/05). Replace this
wholesale then; do not extend it with real-auth features in the meantime.

`X-Org-Id` is required — every RLS-protected query fails closed (returns
nothing / rejects writes) without it, per app/core/db.py's get_session
docstring, so there is no safe default to fall back to. `X-User-Id` is
optional and only ever used as the audit trail's `actor_id` — every FK to it
is nullable, same convention as `Commitment.verified_by`.
"""

import uuid
from typing import Annotated

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.models import Project


async def get_org_id(
    x_org_id: Annotated[
        uuid.UUID,
        Header(description="STUB dev auth: organisation to scope this request to. Not real auth."),
    ],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> uuid.UUID:
    """SELECT set_config(...), is_local=true — scoped to this request's
    transaction so the value can't leak across a pooled connection into a
    different request (see app/core/db.py's get_session docstring; Postgres's
    SET statement doesn't accept bind parameters at all, only the function
    form does)."""
    await session.execute(
        text("SELECT set_config('app.current_org_id', :oid, true)"), {"oid": str(x_org_id)}
    )
    return x_org_id


async def get_actor_id(
    x_user_id: Annotated[
        uuid.UUID | None,
        Header(description="STUB dev auth: acting user, recorded on the audit trail. Not real auth."),
    ] = None,
) -> uuid.UUID | None:
    return x_user_id


async def get_project(
    project_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    org_id: Annotated[uuid.UUID, Depends(get_org_id)],  # noqa: ARG001 — ordering: RLS context must be set first
) -> Project:
    project = (
        await session.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project
