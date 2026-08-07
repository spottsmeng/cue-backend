"""Real per-request identity/RBAC (PRD §6.14, FR-ADM-01 through FR-ADM-05),
replacing the earlier X-Org-Id/X-User-Id dev-auth stub wholesale.

Every RLS-protected query still needs `app.current_org_id` set before it runs
(app/core/db.py's get_session docstring) — the difference is where that value
now comes from: the verified bearer token's `org_id` claim
(app/identity/tokens.py), never a header a caller can simply set by hand.
"""

import uuid
from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.identity.models import Membership, User
from app.identity.service import effective_roles, resolve_user
from app.identity.tokens import TokenVerificationError, get_token_verifier
from app.models import Project

_bearer_scheme = HTTPBearer(
    auto_error=True, description="CUE identity JWT — see app/identity/tokens.py"
)


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_bearer_scheme)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> User:
    """Verifies the bearer token via the provider-pluggable
    app/identity/tokens.py (local HS256 for dev/test, OIDC/JWKS in
    production — a config change, not a code change), sets
    `app.current_org_id` from the token's own `org_id` claim, and
    resolves/provisions the corresponding `users` row (app/identity/service.py's
    resolve_user — SCIM pre-provisioning is out of scope this session, so
    "seen in a verified token" is what creates a user).

    Cached per request by FastAPI's dependency resolution — every other
    dependency in this module that needs the caller's identity depends on
    this one rather than re-verifying the token itself.
    """
    verifier = get_token_verifier()
    try:
        claims = verifier.verify(credentials.credentials)
    except TokenVerificationError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e

    await session.execute(
        text("SELECT set_config('app.current_org_id', :oid, true)"), {"oid": str(claims.org_id)}
    )
    return await resolve_user(session, claims)


async def get_org_id(user: Annotated[User, Depends(get_current_user)]) -> uuid.UUID:
    """What org-scoped-but-not-project-scoped endpoints (project provisioning)
    key off — always the authenticated user's own organisation, never a
    client-supplied value."""
    return user.organisation_id


async def get_actor_id(user: Annotated[User, Depends(get_current_user)]) -> uuid.UUID:
    """The audit trail's `actor_id` (FR-LED-12) — always a real, verified
    user now, never the optional/spoofable header the dev stub allowed."""
    return user.id


def require_project_role(*roles: str):
    """Dependency factory replacing the stub's plain `get_project`.

    The caller must have a membership (any role) on `project_id`, or an
    active delegation onto it, to learn the project exists at all
    (FR-ADM-02) — a non-member gets 404, indistinguishable from a genuinely
    nonexistent project, so membership itself is never leaked by the status
    code. If `roles` is given, the user's *effective* role set (own
    membership role plus any currently-active delegated role — see
    app/identity/service.py's effective_roles) must intersect it, or the
    request is a 403: they can see the project, they just can't do this
    specific thing on it.

    `Depends(require_project_role())` (no roles) is the read-access shape,
    aliased below as `get_project` for the read-only endpoints that used the
    stub's dependency directly; `Depends(require_project_role(*WRITE_ROLES))`
    is what every ledger-mutating endpoint uses.
    """

    async def dependency(
        project_id: uuid.UUID,
        session: Annotated[AsyncSession, Depends(get_session)],
        user: Annotated[User, Depends(get_current_user)],
    ) -> Project:
        project = (
            await session.execute(select(Project).where(Project.id == project_id))
        ).scalar_one_or_none()
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")

        granted = await effective_roles(session, user.id, project_id)
        if not granted:
            raise HTTPException(status_code=404, detail="project not found")
        if roles and not (granted & set(roles)):
            raise HTTPException(status_code=403, detail="insufficient role for this action")
        return project

    return dependency


get_project = require_project_role()


async def require_org_administrator(
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> User:
    """§11.2's /admin/* surfaces (users, roles, delegations, export) need a
    genuinely different access rule from require_project_role() above — not
    a variant reusing its logic, a different question entirely. That
    dependency answers "can this user act on project X", scoped to a
    membership or delegation on that one project; a non-member is 404'd
    before role is even considered. This answers "does this user hold the
    administrator role on *at least one* project in their own
    organisation" — if so, every /admin/* endpoint they reach is then free
    to read/list across every project in that organisation, not just ones
    they happen to be a member of, which is the entire point of an org-wide
    admin surface existing separately from the project-scoped one.

    The membership query below carries no explicit organisation_id filter —
    RLS's own tenant_isolation policy on `memberships` (project-joined,
    since that table has no organisation_id column of its own) already
    confines it to the caller's org, the same "RLS plus an independent
    application-level check, not one replacing the other" split
    app/api/projects.py's list_projects docstring describes for
    accessible_project_ids. A user who is Administrator on project A but has
    no membership at all on project B still passes this check and can read
    B through /admin/*, even though Depends(get_project) on project B
    directly would 404 them — see tests/test_admin_api.py for a test that
    exercises exactly that distinction.
    """
    is_org_administrator = (
        await session.execute(
            select(Membership.id)
            .where(Membership.user_id == user.id, Membership.role == "administrator")
            .limit(1)
        )
    ).scalar_one_or_none()
    if is_org_administrator is None:
        raise HTTPException(
            status_code=403,
            detail="administrator role required on at least one project in this organisation",
        )
    return user
