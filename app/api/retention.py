import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_org_administrator
from app.api.schemas import RetentionPolicyCreate, RetentionPolicyOut, RetentionPolicyUpdate
from app.core.db import get_session
from app.identity.models import User
from app.models import RetentionPolicy, Vertical

router = APIRouter(prefix="/admin/retention", tags=["retention"])


async def _get_policy(session: AsyncSession, policy_id: uuid.UUID) -> RetentionPolicy:
    """No explicit organisation_id filter needed — retention_policies' own
    tenant_isolation policy (direct-column, migration 9ddb100d7e8e, same
    shape as `users`) already confines this to the caller's organisation."""
    policy = (
        await session.execute(select(RetentionPolicy).where(RetentionPolicy.id == policy_id))
    ).scalar_one_or_none()
    if policy is None:
        raise HTTPException(status_code=404, detail="retention policy not found")
    return policy


async def _require_vertical(session: AsyncSession, vertical_id: uuid.UUID | None) -> None:
    if vertical_id is None:
        return
    exists = (
        await session.execute(select(Vertical.id).where(Vertical.id == vertical_id))
    ).scalar_one_or_none()
    if exists is None:
        raise HTTPException(status_code=422, detail=f"vertical_id: no such vertical {vertical_id}")


@router.get("", response_model=list[RetentionPolicyOut])
async def list_retention_policies(
    session: Annotated[AsyncSession, Depends(get_session)],
    _admin: Annotated[User, Depends(require_org_administrator)],
) -> list[RetentionPolicy]:
    """FR-ADM-08: config only — no deletion automation reads this yet (out
    of scope this session, see Prompt 5)."""
    policies = (await session.execute(select(RetentionPolicy))).scalars().all()
    return list(policies)


@router.get("/{policy_id}", response_model=RetentionPolicyOut)
async def read_retention_policy(
    policy_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    _admin: Annotated[User, Depends(require_org_administrator)],
) -> RetentionPolicy:
    return await _get_policy(session, policy_id)


@router.post("", response_model=RetentionPolicyOut, status_code=201)
async def create_retention_policy(
    body: RetentionPolicyCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    admin: Annotated[User, Depends(require_org_administrator)],
) -> RetentionPolicy:
    await _require_vertical(session, body.vertical_id)
    policy = RetentionPolicy(
        organisation_id=admin.organisation_id,
        vertical_id=body.vertical_id,
        region=body.region,
        retention_days=body.retention_days,
    )
    session.add(policy)
    await session.commit()
    return policy


@router.patch("/{policy_id}", response_model=RetentionPolicyOut)
async def update_retention_policy(
    policy_id: uuid.UUID,
    body: RetentionPolicyUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    _admin: Annotated[User, Depends(require_org_administrator)],
) -> RetentionPolicy:
    policy = await _get_policy(session, policy_id)
    changes = body.model_dump(exclude_unset=True)
    if "vertical_id" in changes:
        await _require_vertical(session, changes["vertical_id"])
    for field, value in changes.items():
        setattr(policy, field, value)
    if changes:
        await session.flush()
        # updated_at has a server-side onupdate=func.now() — same
        # MissingGreenlet trap app/api/commitments.py's _refresh_updated_at
        # documents; refresh explicitly before commit rather than let a bare
        # attribute access on the response trigger a lazy-load outside the
        # async greenlet bridge.
        await session.refresh(policy, attribute_names=["updated_at"])
    await session.commit()
    return policy


@router.delete("/{policy_id}", status_code=204)
async def delete_retention_policy(
    policy_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    _admin: Annotated[User, Depends(require_org_administrator)],
) -> None:
    policy = await _get_policy(session, policy_id)
    await session.delete(policy)
    await session.commit()
