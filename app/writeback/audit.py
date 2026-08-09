import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.writeback.models import WritebackAuditLog


async def record_writeback_audit_event(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    action: str,
    actor_id: uuid.UUID | None,
    detail: dict | None = None,
) -> WritebackAuditLog:
    """Mirrors app/ledger/audit.py's record_audit_event and
    app/foresight/audit.py's record_foresight_audit_event exactly — flushes
    so `.id` is available, does not commit; the caller owns the transaction
    boundary."""
    entry = WritebackAuditLog(
        project_id=project_id, action=action, actor_id=actor_id, detail=detail or {},
    )
    session.add(entry)
    await session.flush()
    return entry
