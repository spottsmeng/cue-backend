import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.twin.models import TwinAuditLog


async def record_twin_audit_event(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    action: str,
    actor_id: uuid.UUID | None,
    milestone_id: uuid.UUID | None = None,
    dependency_id: uuid.UUID | None = None,
    detail: dict | None = None,
) -> TwinAuditLog:
    """FR-TWN-10's "recorded as a project-specific adjustment, not a silent
    overwrite". Mirrors app/ledger/audit.py's record_audit_event exactly —
    flushes so `.id` is available, does not commit; the caller owns the
    transaction boundary."""
    entry = TwinAuditLog(
        project_id=project_id,
        milestone_id=milestone_id,
        dependency_id=dependency_id,
        action=action,
        actor_id=actor_id,
        detail=detail or {},
    )
    session.add(entry)
    await session.flush()
    return entry
