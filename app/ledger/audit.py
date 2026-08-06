import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog


async def record_audit_event(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    commitment_id: uuid.UUID,
    action: str,
    actor_id: uuid.UUID | None,
    from_state: str | None = None,
    to_state: str | None = None,
    evidence_id: uuid.UUID | None = None,
    detail: dict | None = None,
) -> AuditLog:
    """FR-LED-12. Callers flush the rest of their own transaction; this only
    adds and flushes the audit row so `.id` is available if needed, it does
    not commit — same transaction-boundary convention as
    app/ledger/extractor.py's extract_case."""
    entry = AuditLog(
        project_id=project_id,
        commitment_id=commitment_id,
        action=action,
        actor_id=actor_id,
        from_state=from_state,
        to_state=to_state,
        evidence_id=evidence_id,
        detail=detail or {},
    )
    session.add(entry)
    await session.flush()
    return entry
