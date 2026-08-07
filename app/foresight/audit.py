import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.foresight.models import ForesightAuditLog


async def record_foresight_audit_event(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    action: str,
    actor_id: uuid.UUID | None,
    risk_id: uuid.UUID | None = None,
    deviation_id: uuid.UUID | None = None,
    notification_id: uuid.UUID | None = None,
    detail: dict | None = None,
) -> ForesightAuditLog:
    """Mirrors app/ledger/audit.py's record_audit_event and
    app/twin/audit.py's record_twin_audit_event exactly — flushes so `.id`
    is available, does not commit; the caller owns the transaction boundary.
    `actor_id=None` for every system-triggered write (a Silence Radar
    finding, an automatic lifecycle transition) — same convention
    app/ledger/extractor.py establishes for "this write has no human behind
    it."""
    entry = ForesightAuditLog(
        project_id=project_id,
        risk_id=risk_id,
        deviation_id=deviation_id,
        notification_id=notification_id,
        action=action,
        actor_id=actor_id,
        detail=detail or {},
    )
    session.add(entry)
    await session.flush()
    return entry
