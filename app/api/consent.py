import csv
import io
import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_org_administrator
from app.api.schemas import ConsentActionRequest, ConsentRecordOut, ConsentStatusLiteral
from app.capture.consent import upsert_consent_record
from app.core.db import get_session
from app.identity.models import User
from app.models import ConsentRecord, Party, Project

router = APIRouter(prefix="/admin/consent", tags=["consent"])

_CSV_FIELDS = [
    "id", "party_id", "project_id", "notice_sent_at", "status", "evidence",
    "created_at", "updated_at",
]


def _jsonable(value: object) -> object:
    return value.isoformat() if isinstance(value, datetime) else value


def _to_csv_row(record: ConsentRecord) -> dict[str, object]:
    return {field: _jsonable(getattr(record, field)) for field in _CSV_FIELDS}


async def _list_consent_records(
    session: AsyncSession,
    *,
    project_id: uuid.UUID | None,
    party_id: uuid.UUID | None,
    status: str | None,
) -> list[ConsentRecord]:
    """No explicit organisation filter — consent_records' own tenant_isolation
    policy (project-joined, migration 9ddb100d7e8e) already confines this to
    the caller's organisation, same as every other RLS-scoped query in this
    module."""
    stmt = select(ConsentRecord)
    if project_id is not None:
        stmt = stmt.where(ConsentRecord.project_id == project_id)
    if party_id is not None:
        stmt = stmt.where(ConsentRecord.party_id == party_id)
    if status is not None:
        stmt = stmt.where(ConsentRecord.status == status)
    stmt = stmt.order_by(ConsentRecord.created_at.desc())
    return list((await session.execute(stmt)).scalars().all())


@router.get("", response_model=list[ConsentRecordOut])
async def list_consent(
    session: Annotated[AsyncSession, Depends(get_session)],
    _admin: Annotated[User, Depends(require_org_administrator)],
    project_id: uuid.UUID | None = None,
    party_id: uuid.UUID | None = None,
    status: ConsentStatusLiteral | None = None,
) -> list[ConsentRecord]:
    """FR-ADM-07: view the consent ledger — org-wide, not limited to
    projects the caller happens to be a member of (require_org_administrator,
    not require_project_role)."""
    return await _list_consent_records(session, project_id=project_id, party_id=party_id, status=status)


@router.get("/export", response_model=None)
async def export_consent(
    session: Annotated[AsyncSession, Depends(get_session)],
    _admin: Annotated[User, Depends(require_org_administrator)],
    project_id: uuid.UUID | None = None,
    party_id: uuid.UUID | None = None,
    status: ConsentStatusLiteral | None = None,
    format: Literal["json", "csv"] = "json",
) -> Response:
    """FR-ADM-07's 'export' verb — same filters and scope as list, just a
    different wire format for handing the ledger to a data-protection
    officer or a regulator. Returns a real Response object either way
    (response_model=None disables FastAPI's usual return-type-driven
    serialization) so the csv branch isn't forced through JSON encoding."""
    records = await _list_consent_records(session, project_id=project_id, party_id=party_id, status=status)
    if format == "json":
        return JSONResponse(
            content=[ConsentRecordOut.model_validate(r).model_dump(mode="json") for r in records]
        )

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=_CSV_FIELDS)
    writer.writeheader()
    for record in records:
        writer.writerow(_to_csv_row(record))
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=consent_records.csv"},
    )


@router.post("/action-request", response_model=ConsentRecordOut, status_code=201)
async def consent_action_request(
    body: ConsentActionRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    admin: Annotated[User, Depends(require_org_administrator)],
) -> ConsentRecord:
    """FR-ADM-07's 'action data-subject requests' — records the outcome of a
    party's consent notice, or a later objection/opt-out, against the ledger.
    Upsert-by-(party_id, project_id), same idempotent-by-natural-key shape
    app/api/projects.py's _grant_membership already uses for Membership: a
    party's consent state is one current row, not an append-only history,
    since FR-CAP-07's 'honour opt-out immediately' means only the current
    status is ever actually acted on."""
    project = (
        await session.execute(select(Project).where(Project.id == body.project_id))
    ).scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=422, detail=f"project_id: no such project {body.project_id}")

    # parties has no RLS of its own (same gap app/api/commitments.py's
    # _require_party documents) — the organisation_id filter here is what
    # actually confines this lookup to the caller's own tenant.
    party = (
        await session.execute(
            select(Party.id).where(
                Party.id == body.party_id, Party.organisation_id == admin.organisation_id
            )
        )
    ).scalar_one_or_none()
    if party is None:
        raise HTTPException(status_code=422, detail=f"party_id: no such party {body.party_id}")

    record = await upsert_consent_record(
        session,
        party_id=body.party_id,
        project_id=body.project_id,
        status=body.status,
        evidence=body.evidence,
        notice_sent_at=body.notice_sent_at,
    )
    await session.commit()
    return record
