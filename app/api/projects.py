import uuid
from datetime import datetime, timezone as dt_timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_project, require_project_role
from app.api.schemas import (
    DelegationCreate,
    DelegationOut,
    MembershipCreate,
    MembershipOut,
    ProjectCreate,
    ProjectOut,
)
from app.core.db import get_session
from app.identity.models import Delegation, Membership, User
from app.identity.service import ADMIN_ROLES, accessible_project_ids, effective_roles
from app.models import Project, Vertical

router = APIRouter(prefix="/projects", tags=["projects"])


async def _resolve_member(session: AsyncSession, email: str) -> User:
    """No SCIM pre-provisioning this session (PRD's explicit out-of-scope
    note) — a member can only be assigned by email once they've signed in at
    least once, since that's what creates their `users` row
    (app/identity/service.py's resolve_user). RLS (users has its own policy,
    same as every other tenant-scoped table) is what actually confines this
    lookup to the caller's own organisation; no app-level organisation_id
    filter needed on top of it, unlike `parties` which has no RLS of its own."""
    user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=422, detail=f"no user with email {email!r} has signed in yet"
        )
    return user


async def _grant_membership(
    session: AsyncSession, project_id: uuid.UUID, user_id: uuid.UUID, role: str, granted_by: uuid.UUID
) -> Membership:
    """Idempotent by (user_id, project_id) — re-assigning an existing member
    updates their role rather than erroring or duplicating (memberships'
    unique constraint would reject a duplicate insert outright)."""
    existing = (
        await session.execute(
            select(Membership).where(
                Membership.user_id == user_id, Membership.project_id == project_id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.role = role
        existing.granted_by = granted_by
        await session.flush()
        return existing

    membership = Membership(user_id=user_id, project_id=project_id, role=role, granted_by=granted_by)
    session.add(membership)
    await session.flush()
    return membership


@router.post("", response_model=ProjectOut, status_code=201)
async def create_project(
    body: ProjectCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> Project:
    """FR-ADM-06: provisioning and initial member assignment in one call.
    Any authenticated member of the organisation may provision a project —
    there is no org-level role table (membership is project-scoped only, per
    the PRD's ER model), so the creator is granted "administrator" membership
    on their own new project automatically, and can assign further members
    from there (this endpoint's `members` list, or POST .../members later)."""
    vertical = (
        await session.execute(select(Vertical).where(Vertical.code == body.vertical_code))
    ).scalar_one_or_none()
    if vertical is None:
        raise HTTPException(
            status_code=422, detail=f"unknown vertical_code: {body.vertical_code!r}"
        )

    project = Project(
        organisation_id=user.organisation_id,
        vertical_id=vertical.id,
        name=body.name,
        client_name=body.client_name,
        venue=body.venue,
        timezone=body.timezone,
    )
    session.add(project)
    await session.flush()  # need project.id for the membership rows below

    await _grant_membership(session, project.id, user.id, "administrator", user.id)
    for member in body.members:
        target = await _resolve_member(session, member.email)
        await _grant_membership(session, project.id, target.id, member.role, user.id)

    await session.commit()
    # No session.refresh() here: same reasoning as before this session —
    # app.current_org_id (is_local=true) doesn't survive past the commit, and
    # expire_on_commit=False means it isn't needed anyway.
    return project


@router.get("", response_model=list[ProjectOut])
async def list_projects(
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> list[Project]:
    """FR-ADM-02: scoped to projects this user is a member of, or currently
    delegated into — an *application-level* filter on top of (not a
    replacement for) RLS's own org-level isolation, which
    Depends(get_current_user) already set up by putting app.current_org_id in
    place. The two are tested as independent properties, not one, in
    tests/test_membership_scoping.py."""
    project_ids = await accessible_project_ids(session, user.id)
    if not project_ids:
        return []
    projects = (
        await session.execute(
            select(Project).where(Project.id.in_(project_ids)).order_by(Project.created_at.desc())
        )
    ).scalars().all()
    return list(projects)


@router.get("/{project_id}", response_model=ProjectOut)
async def read_project(project: Annotated[Project, Depends(get_project)]) -> Project:
    return project


@router.post("/{project_id}/members", response_model=MembershipOut, status_code=201)
async def add_member(
    body: MembershipCreate,
    project: Annotated[Project, Depends(require_project_role(*ADMIN_ROLES))],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> Membership:
    """FR-ADM-06's 'assign members' step, standalone (not just at
    provisioning time) — administrator or producer only."""
    target = await _resolve_member(session, body.email)
    membership = await _grant_membership(session, project.id, target.id, body.role, user.id)
    await session.commit()
    return membership


@router.post("/{project_id}/delegations", response_model=DelegationOut, status_code=201)
async def grant_delegation(
    body: DelegationCreate,
    project: Annotated[Project, Depends(get_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> Delegation:
    """FR-ADM-03/04. A delegator may only lend a role they themselves
    currently hold (their own membership role, or a role already delegated
    to them) — except an administrator, who may delegate any role on this
    project (e.g. arranging cover for someone else's absence). The DB layer
    (this migration's delegation_append_only trigger) is FR-ADM-04's audit
    trail; nothing further to log here."""
    granted = await effective_roles(session, user.id, project.id)
    if body.role not in granted and "administrator" not in granted:
        raise HTTPException(status_code=403, detail="cannot delegate a role you do not hold")

    delegate = await _resolve_member(session, body.delegate_email)
    if delegate.id == user.id:
        raise HTTPException(status_code=422, detail="cannot delegate to yourself")
    if body.expires_at <= datetime.now(dt_timezone.utc):
        raise HTTPException(status_code=422, detail="expires_at must be in the future")

    delegation = Delegation(
        project_id=project.id,
        delegator_id=user.id,
        delegate_id=delegate.id,
        role=body.role,
        granted_by=user.id,
        expires_at=body.expires_at,
    )
    session.add(delegation)
    await session.commit()
    return delegation


@router.post("/{project_id}/delegations/{delegation_id}/revoke", response_model=DelegationOut)
async def revoke_delegation(
    delegation_id: uuid.UUID,
    project: Annotated[Project, Depends(get_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> Delegation:
    """Early revocation (FR-ADM-04's 'when it expired', the early-termination
    case distinct from a delegation simply running past its natural
    expires_at) — only the delegator themselves or an administrator may do
    this."""
    delegation = (
        await session.execute(
            select(Delegation).where(
                Delegation.id == delegation_id, Delegation.project_id == project.id
            )
        )
    ).scalar_one_or_none()
    if delegation is None:
        raise HTTPException(status_code=404, detail="delegation not found")
    if delegation.revoked_at is not None:
        raise HTTPException(status_code=409, detail="delegation already revoked")

    granted = await effective_roles(session, user.id, project.id)
    if user.id != delegation.delegator_id and "administrator" not in granted:
        raise HTTPException(
            status_code=403, detail="only the delegator or an administrator may revoke"
        )

    delegation.revoked_at = datetime.now(dt_timezone.utc)
    delegation.revoked_by = user.id
    await session.commit()
    return delegation
