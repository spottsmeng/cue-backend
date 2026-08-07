"""RBAC decisions: resolving a verified token to a `User` row, and answering
"what can this user do on this project right now" — the two questions every
request-scoped dependency in app/api/deps.py needs answered before it lets a
request through.
"""

import uuid
from datetime import datetime, timezone as dt_timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.identity.models import Delegation, Membership, User
from app.identity.tokens import TokenClaims

# FR-ADM-01's roles minus "read_only" — read_only exists specifically to grant
# visibility without write access, so it must never appear here. "delegate" is
# also excluded: holding that role on a membership grants no standing
# permission of its own, only whatever a currently-active Delegation adds to
# this user's effective set (see effective_roles below) — that's the entire
# point of the role existing as a distinct value from the others.
WRITE_ROLES: frozenset[str] = frozenset(
    {"project_manager", "producer", "finance", "account_manager", "designer", "administrator"}
)

# Who may provision new projects and manage membership on them (FR-ADM-06).
ADMIN_ROLES: frozenset[str] = frozenset({"administrator", "producer"})

# Budget baseline / payment_status (FR-ADM-11, FR-LED-13). The PRD's own
# persona table merges "Finance" and "Procurement" into one persona — there
# is no distinct native role for each in FR-ADM-01's fixed list, so this tier
# is modelled as {"finance", "producer"} rather than inventing a ninth role.
# Procurement access (needed later, for the Vendor Reliability Graph
# milestone) reuses this same tier.
FINANCE_ROLES: frozenset[str] = frozenset({"finance", "producer"})


async def resolve_user(session: AsyncSession, claims: TokenClaims) -> User:
    """Upsert-by-claims: the first request from a given (issuer, sub) creates
    the row (SCIM pre-provisioning is explicitly out of scope this session —
    CUE-PRD.md's FR-ADM-05 note — so "seen in a verified token" is what
    provisions a user instead), later requests just refresh email/name in
    case the IdP's profile changed. Never trusts the caller for anything
    other than what the verified token itself asserts.

    Callers must have already set `app.current_org_id` for this transaction
    (see app/core/db.py's get_session docstring) — this INSERT relies on RLS
    to reject a claims.org_id that doesn't match, same fail-closed posture as
    every other tenant-scoped write in this codebase.
    """
    existing = (
        await session.execute(
            select(User).where(User.issuer == claims.issuer, User.external_subject == claims.subject)
        )
    ).scalar_one_or_none()
    if existing is not None:
        if claims.email and existing.email != claims.email:
            existing.email = claims.email
        if claims.name and existing.display_name != claims.name:
            existing.display_name = claims.name
        await session.flush()
        return existing

    user = User(
        organisation_id=claims.org_id,
        issuer=claims.issuer,
        external_subject=claims.subject,
        email=claims.email or f"{claims.subject}@unknown.invalid",
        display_name=claims.name,
    )
    session.add(user)
    await session.flush()
    return user


async def effective_roles(session: AsyncSession, user_id: uuid.UUID, project_id: uuid.UUID) -> set[str]:
    """Union of a user's own project membership role and every role currently
    delegated to them on this project (FR-ADM-03: "a delegate temporarily
    gains a role's access"). Checked at request time against `now()`, not a
    background sweep (see app/identity/models.py's Delegation docstring) — an
    expired or revoked delegation simply stops contributing to this set on
    the very next call, nothing else to invalidate.
    """
    roles: set[str] = set()

    membership_role = (
        await session.execute(
            select(Membership.role).where(
                Membership.user_id == user_id, Membership.project_id == project_id
            )
        )
    ).scalar_one_or_none()
    if membership_role is not None:
        roles.add(membership_role)

    delegated_roles = (
        await session.execute(
            select(Delegation.role).where(
                Delegation.delegate_id == user_id,
                Delegation.project_id == project_id,
                Delegation.revoked_at.is_(None),
                Delegation.expires_at > func.now(),
            )
        )
    ).scalars().all()
    roles.update(delegated_roles)

    return roles


async def accessible_project_ids(session: AsyncSession, user_id: uuid.UUID) -> set[uuid.UUID]:
    """FR-ADM-02's application-level filter: every project this user can see,
    via standing membership or an active delegation — the set list_projects
    (app/api/projects.py) intersects against RLS's own org-scoped result, as
    two independently-enforced properties rather than one."""
    member_of = (
        await session.execute(select(Membership.project_id).where(Membership.user_id == user_id))
    ).scalars().all()
    delegated_into = (
        await session.execute(
            select(Delegation.project_id).where(
                Delegation.delegate_id == user_id,
                Delegation.revoked_at.is_(None),
                Delegation.expires_at > func.now(),
            )
        )
    ).scalars().all()
    return set(member_of) | set(delegated_into)


def utcnow() -> datetime:
    return datetime.now(dt_timezone.utc)
