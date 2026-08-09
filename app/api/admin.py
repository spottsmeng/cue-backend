"""§11.2's org-wide admin surfaces: /admin/users, /admin/roles,
/admin/delegations (completing the project-scoped versions
app/api/projects.py already built) and /admin/export (FR-ADM-10).

Every endpoint here is gated by require_org_administrator, not
require_project_role — see that dependency's docstring (app/api/deps.py) for
why an org-wide admin surface is a deliberately different access rule from
the project-scoped one, not a variant of it.
"""

import csv
import io
import uuid
import zipfile
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_org_administrator
from app.api.schemas import (
    CostSummaryOut,
    CostSummaryRow,
    DelegationOut,
    MembershipOut,
    MembershipRoleLiteral,
    UserOut,
)
from app.core.db import get_session
from app.identity.models import Delegation, Membership, User
from app.llm.models import LLMUsageEvent
from app.models import AuditLog, Budget, Commitment, Evidence, Project

router = APIRouter(prefix="/admin", tags=["admin"])


def _jsonable(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


@router.get("/users", response_model=list[UserOut])
async def list_org_users(
    session: Annotated[AsyncSession, Depends(get_session)],
    _admin: Annotated[User, Depends(require_org_administrator)],
) -> list[User]:
    """No SCIM pre-provisioning this session — this only ever lists users
    who have actually authenticated at least once (app/identity/service.py's
    resolve_user is the only thing that creates a `users` row)."""
    users = (
        await session.execute(select(User).order_by(User.created_at.desc()))
    ).scalars().all()
    return list(users)


@router.get("/users/{user_id}", response_model=UserOut)
async def read_org_user(
    user_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    _admin: Annotated[User, Depends(require_org_administrator)],
) -> User:
    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    return user


@router.get("/roles", response_model=list[MembershipOut])
async def list_org_roles(
    session: Annotated[AsyncSession, Depends(get_session)],
    _admin: Annotated[User, Depends(require_org_administrator)],
    project_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    role: MembershipRoleLiteral | None = None,
) -> list[Membership]:
    """Org-wide view of every role assignment across every project in the
    caller's organisation — distinct from GET .../projects/{id}/members
    (app/api/projects.py), which only ever sees one project at a time and
    requires the caller to be a member of it."""
    stmt = select(Membership)
    if project_id is not None:
        stmt = stmt.where(Membership.project_id == project_id)
    if user_id is not None:
        stmt = stmt.where(Membership.user_id == user_id)
    if role is not None:
        stmt = stmt.where(Membership.role == role)
    memberships = (await session.execute(stmt)).scalars().all()
    return list(memberships)


@router.get("/delegations", response_model=list[DelegationOut])
async def list_org_delegations(
    session: Annotated[AsyncSession, Depends(get_session)],
    _admin: Annotated[User, Depends(require_org_administrator)],
    project_id: uuid.UUID | None = None,
    active_only: bool = False,
) -> list[Delegation]:
    """FR-ADM-04's full audit of delegation grants, org-wide."""
    stmt = select(Delegation)
    if project_id is not None:
        stmt = stmt.where(Delegation.project_id == project_id)
    if active_only:
        stmt = stmt.where(Delegation.revoked_at.is_(None), Delegation.expires_at > func.now())
    stmt = stmt.order_by(Delegation.granted_at.desc())
    delegations = (await session.execute(stmt)).scalars().all()
    return list(delegations)


async def _get_org_project(session: AsyncSession, project_id: uuid.UUID) -> Project:
    """Deliberately not require_project_role/get_project — those 404 a
    caller who isn't a member of this specific project, exactly the
    membership check an org-wide admin export must NOT apply (see
    require_org_administrator's docstring). `projects` RLS (organisation_id-
    scoped) is what still stops this from reaching another tenant's
    project."""
    project = (await session.execute(select(Project).where(Project.id == project_id))).scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


async def _export_bundle(session: AsyncSession, project: Project) -> dict[str, list[dict]]:
    """FR-ADM-10: 'export a complete project record (ledger, documents,
    audit)'. Documents skipped this session — that milestone doesn't exist
    yet, per Prompt 5's own scope note."""
    commitments = (
        (await session.execute(select(Commitment).where(Commitment.project_id == project.id)))
        .scalars()
        .all()
    )
    commitment_ids = [c.id for c in commitments]
    evidence = (
        (
            await session.execute(
                select(Evidence).where(Evidence.commitment_id.in_(commitment_ids))
            )
        )
        .scalars()
        .all()
        if commitment_ids
        else []
    )
    budgets = (
        (await session.execute(select(Budget).where(Budget.project_id == project.id)))
        .scalars()
        .all()
    )
    audit_log = (
        (
            await session.execute(
                select(AuditLog).where(AuditLog.project_id == project.id).order_by(AuditLog.occurred_at)
            )
        )
        .scalars()
        .all()
    )

    def _row(obj: object, fields: list[str]) -> dict:
        return {f: _jsonable(getattr(obj, f)) for f in fields}

    return {
        "project": [
            _row(
                project,
                [
                    "id", "organisation_id", "vertical_id", "name", "client_name", "venue",
                    "timezone", "event_start", "event_end", "archived_at", "created_at", "updated_at",
                ],
            )
        ],
        "commitments": [
            _row(
                c,
                [
                    "id", "project_id", "party_id", "counterparty_id", "deliverable_id", "act_type_id",
                    "state", "deliverable_en", "deliverable_original", "due_at", "amount", "currency",
                    "payment_status", "confidence", "verification_state", "verified_by", "verified_at",
                    "created_at", "updated_at",
                ],
            )
            for c in commitments
        ],
        "evidence": [
            _row(
                e,
                [
                    "id", "commitment_id", "budget_id", "channel", "sent_at", "language",
                    "original_text", "translation", "span_start", "span_end",
                ],
            )
            for e in evidence
        ],
        "budgets": [
            _row(
                b,
                [
                    "id", "project_id", "approved_amount", "currency", "approved_by", "approved_at",
                    "revision_of", "is_current",
                ],
            )
            for b in budgets
        ],
        "audit_log": [
            _row(
                a,
                [
                    "id", "project_id", "commitment_id", "action", "actor_id", "occurred_at",
                    "from_state", "to_state", "evidence_id",
                ],
            )
            for a in audit_log
        ],
    }


@router.get("/export/{project_id}", response_model=None)
async def export_project(
    project_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    _admin: Annotated[User, Depends(require_org_administrator)],
    format: Literal["json", "csv"] = "json",
) -> Response:
    """FR-ADM-10: a project's full ledger + audit record as a structured
    bundle. Any project in the caller's own organisation, not just ones
    they're a member of — the org-wide admin surface this whole router
    exists for."""
    project = await _get_org_project(session, project_id)
    bundle = await _export_bundle(session, project)

    if format == "json":
        return JSONResponse(content=bundle)

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for table_name, rows in bundle.items():
            csv_buffer = io.StringIO()
            fieldnames = list(rows[0].keys()) if rows else []
            writer = csv.DictWriter(csv_buffer, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
            archive.writestr(f"{table_name}.csv", csv_buffer.getvalue())

    return Response(
        content=zip_buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=project_{project.id}_export.zip"},
    )


@router.get("/cost-summary", response_model=CostSummaryOut)
async def read_cost_summary(
    session: Annotated[AsyncSession, Depends(get_session)],
    _admin: Annotated[User, Depends(require_org_administrator)],
    project_id: uuid.UUID | None = None,
) -> CostSummaryOut:
    """NFR-OBS-03 / PRD §13's "cost per active project" row, made real for
    the first time — app/llm/cost.py's record_llm_usage has written real
    LLMUsageEvent rows since the Hardening session (backend/PROGRESS.md's
    M10), attributed per project/organisation on every extraction/ask/
    contradiction/write-back call, but nothing ever read them back over
    the API until now. llm_usage_events carries its own tenant_isolation
    RLS policy (migration 0a76bb463d69), so — unlike `parties`, which has
    none — no explicit organisation_id filter is needed here; RLS already
    confines every row this query can see to the caller's own org.
    """
    stmt = (
        select(
            LLMUsageEvent.project_id,
            LLMUsageEvent.provider,
            LLMUsageEvent.model,
            func.count(LLMUsageEvent.id).label("call_count"),
            func.coalesce(func.sum(LLMUsageEvent.tokens_in), 0).label("tokens_in"),
            func.coalesce(func.sum(LLMUsageEvent.tokens_out), 0).label("tokens_out"),
            func.sum(LLMUsageEvent.estimated_cost_usd).label("estimated_cost_usd"),
        )
        .group_by(LLMUsageEvent.project_id, LLMUsageEvent.provider, LLMUsageEvent.model)
        .order_by(LLMUsageEvent.project_id, LLMUsageEvent.provider, LLMUsageEvent.model)
    )
    if project_id is not None:
        stmt = stmt.where(LLMUsageEvent.project_id == project_id)
    result = (await session.execute(stmt)).all()

    rows = [
        CostSummaryRow(
            project_id=r.project_id,
            provider=r.provider,
            model=r.model,
            call_count=r.call_count,
            tokens_in=r.tokens_in,
            tokens_out=r.tokens_out,
            estimated_cost_usd=(
                float(r.estimated_cost_usd) if r.estimated_cost_usd is not None else None
            ),
        )
        for r in result
    ]
    known_costs = [row.estimated_cost_usd for row in rows if row.estimated_cost_usd is not None]
    return CostSummaryOut(
        rows=rows,
        total_calls=sum(row.call_count for row in rows),
        total_estimated_cost_usd=sum(known_costs) if known_costs else None,
    )
