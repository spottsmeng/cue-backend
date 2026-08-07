import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.foresight.audit import record_foresight_audit_event
from app.foresight.models import Risk

# FR-FOR-10: "Suppress duplicate and superseded findings; collapse related
# findings into one item." This is item 4a's own distinction from FR-NTF-03
# (notification-level collapsing, app/foresight/notification.py) — this
# module is the Risk-level check, before a Notification is ever created.
# app/foresight/silence.py, contradiction.py and forecast.py all call
# create_or_supersede_risk below rather than each rolling its own
# existing-Risk check — per Prompt 7's own explicit instruction to put this
# in "one shared helper ... not three separate ad hoc checks."


async def get_open_risk(
    session: AsyncSession, *, project_id: uuid.UUID, source: str, finding_key: str
) -> Risk | None:
    stmt = select(Risk).where(
        Risk.project_id == project_id,
        Risk.source == source,
        Risk.finding_key == finding_key,
        Risk.status == "open",
    )
    return (await session.execute(stmt)).scalar_one_or_none()


def _material_change(
    existing: Risk, *, severity: str, downstream_consequence: str, base_rate: float | None, detail: dict
) -> bool:
    return (
        existing.severity != severity
        or existing.downstream_consequence != downstream_consequence
        or existing.base_rate != base_rate
        or existing.detail != detail
    )


async def create_or_supersede_risk(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    source: str,
    finding_key: str,
    severity: str,
    downstream_consequence: str,
    base_rate: float | None = None,
    commitment_id: uuid.UUID | None = None,
    milestone_id: uuid.UUID | None = None,
    spec_claim_id: uuid.UUID | None = None,
    detail: dict | None = None,
) -> tuple[Risk, bool]:
    """The single Risk-creation entry point for every detector. Looks up an
    existing *open* Risk for this exact (project, source, finding_key):

    - None exists: insert a fresh Risk, audited `risk_created`.
    - One exists and nothing about it has materially changed since last
      scan (same severity/consequence/base_rate/detail): no-op — this is
      what stops a periodic sweep from creating a new row (and cascading
      into a new Notification) every single run for a condition that simply
      hasn't changed. Returns the existing row, `created=False`.
    - One exists and something material *has* changed (severity escalated,
      a freshly computable base_rate, an updated consequence): the old row
      is marked `superseded` (`superseded_by` points at the new row,
      audited `risk_superseded`) and a new row is inserted and audited
      `risk_created` — a superseded finding is retired, not duplicated
      alongside a live one, but its own history is preserved rather than
      being overwritten in place.

    Does not commit — caller (the detector's own scan function) owns the
    transaction boundary, same convention every other service function in
    this codebase follows.
    """
    existing = await get_open_risk(session, project_id=project_id, source=source, finding_key=finding_key)
    detail = detail or {}

    if existing is not None and not _material_change(
        existing, severity=severity, downstream_consequence=downstream_consequence,
        base_rate=base_rate, detail=detail,
    ):
        return existing, False

    new_risk = Risk(
        project_id=project_id,
        source=source,
        finding_key=finding_key,
        severity=severity,
        status="open",
        commitment_id=commitment_id,
        milestone_id=milestone_id,
        spec_claim_id=spec_claim_id,
        downstream_consequence=downstream_consequence,
        base_rate=base_rate,
        detail=detail,
    )
    session.add(new_risk)
    await session.flush()

    if existing is not None:
        existing.status = "superseded"
        existing.superseded_by = new_risk.id
        await session.flush()
        await record_foresight_audit_event(
            session, project_id=project_id, action="risk_superseded", actor_id=None,
            risk_id=existing.id, detail={"superseded_by": str(new_risk.id)},
        )

    await record_foresight_audit_event(
        session, project_id=project_id, action="risk_created", actor_id=None, risk_id=new_risk.id,
        detail={"source": source, "finding_key": finding_key, "superseded": str(existing.id) if existing else None},
    )
    return new_risk, True
