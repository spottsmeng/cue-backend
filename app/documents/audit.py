import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.documents.models import DocumentAuditLog


async def record_document_audit_event(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    action: str,
    actor_id: uuid.UUID | None,
    document_id: uuid.UUID | None = None,
    document_version_id: uuid.UUID | None = None,
    detail: dict | None = None,
) -> DocumentAuditLog:
    """Mirrors app/ledger/audit.py's record_audit_event and
    app/twin/audit.py's record_twin_audit_event exactly: flushes so `.id` is
    available, does not commit — the caller owns the transaction boundary."""
    entry = DocumentAuditLog(
        project_id=project_id,
        document_id=document_id,
        document_version_id=document_version_id,
        action=action,
        actor_id=actor_id,
        detail=detail or {},
    )
    session.add(entry)
    await session.flush()
    return entry
