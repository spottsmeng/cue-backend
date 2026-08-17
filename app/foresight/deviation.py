import uuid
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.foresight.audit import record_foresight_audit_event
from app.foresight.models import Deviation, Risk
from app.foresight.notification import dispatch_event
from app.models import Evidence, OntologyTerm, Project

# FR-DEV (PRD §6.7): Deviation CRUD service layer — manual creation,
# auto-draft (called from the detectors: silence.py's broken-commitment
# path, contradiction.py, forecast.py), and the "PM confirms or edits"
# two-step (FR-DEV-04) that mirrors app/api/commitments.py's
# verify_commitment exactly (a `changes` before/after diff, distinctly
# labelled from a plain confirmation in the audit trail).


class UnknownDeviationClass(LookupError):
    """A deviation_class code that isn't seeded for this project's vertical
    — a routine 422-worthy input error for the manual-create path, and an
    internal bug (a detector using a code that was never seeded) for the
    auto-draft path."""


def _jsonable(value: object) -> object:
    """Mirrors app/api/commitments.py's _jsonable — detail/JSONB columns go
    through stdlib json.dumps, which chokes on Decimal/datetime/UUID."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


async def get_deviation_class_term(session: AsyncSession, project: Project, code: str) -> OntologyTerm:
    """deviation_class is vertical-scoped (seed_data/deviation_classes.py's
    own docstring), so this needs the same project-aware, most-specific-tier
    resolution app/twin/service.py's get_milestone_type_term uses for
    milestone_type — reimplemented locally (rather than importing that
    module's private _resolve_ontology_terms) to keep Foresight decoupled
    from Twin's internals; the *query shape* is deliberately the same."""
    stmt = select(OntologyTerm).where(
        OntologyTerm.category == "deviation_class",
        OntologyTerm.code == code,
        or_(
            and_(OntologyTerm.vertical_id.is_(None), OntologyTerm.organisation_id.is_(None)),
            and_(
                OntologyTerm.vertical_id == project.vertical_id, OntologyTerm.organisation_id.is_(None)
            ),
            and_(
                OntologyTerm.vertical_id == project.vertical_id,
                OntologyTerm.organisation_id == project.organisation_id,
            ),
        ),
    )
    candidates = (await session.execute(stmt)).scalars().all()
    if not candidates:
        raise UnknownDeviationClass(f"deviation_class ontology term not seeded for this project: {code!r}")

    def _specificity(t: OntologyTerm) -> int:
        return (1 if t.organisation_id is not None else 0) + (1 if t.vertical_id is not None else 0)

    return max(candidates, key=_specificity)


async def draft_deviation(
    session: AsyncSession,
    *,
    project: Project,
    class_code: str,
    description_en: str,
    risk: Risk | None = None,
    commitment_id: uuid.UUID | None = None,
    milestone_id: uuid.UUID | None = None,
    evidence_text: str,
    detail: dict | None = None,
) -> Deviation:
    """FR-DEV-04: auto-draft, called by a detector that has already found a
    qualifying event. Evidence is NOT NULL (deferred trigger, same mechanism
    as Commitment/Budget/DocumentVersion/SpecClaim) — satisfied by an
    Evidence row naming the detection itself as the source, same
    "manually-entered-but-fully-real" posture FR-LED-10 established for
    caller-supplied provenance with no captured message to point at."""
    class_term = await get_deviation_class_term(session, project, class_code)

    deviation = Deviation(
        project_id=project.id,
        class_term_id=class_term.id,
        status="auto_drafted",
        description_en=description_en,
        risk_id=risk.id if risk is not None else None,
        commitment_id=commitment_id,
        milestone_id=milestone_id,
        detail=detail or {},
    )
    session.add(deviation)
    await session.flush()  # need deviation.id for the evidence row below

    evidence = Evidence(
        deviation_id=deviation.id,
        channel="manual",
        sent_at=datetime.now(dt_timezone.utc),
        language="en",
        original_text=evidence_text,
    )
    session.add(evidence)
    await session.flush()

    await record_foresight_audit_event(
        session, project_id=project.id, action="deviation_drafted", actor_id=None,
        deviation_id=deviation.id, risk_id=risk.id if risk is not None else None,
        detail={"class_code": class_code},
    )
    await dispatch_event(session, project=project, event_type="deviation", deviation=deviation)
    return deviation


async def create_manual_deviation(
    session: AsyncSession,
    *,
    project: Project,
    actor_id: uuid.UUID,
    class_code: str,
    description_en: str,
    commitment_id: uuid.UUID | None = None,
    milestone_id: uuid.UUID | None = None,
    original_text: str,
) -> Deviation:
    """FR-DEV-01: manual entry — a PM logging a scope change/delay/
    corrective action CUE's own detection didn't (or couldn't) catch.
    Immediately `confirmed` (a human authored it directly; there is nothing
    to confirm-or-edit about your own entry), same "manually-entered-but-
    fully-real" posture as create_commitment's FR-LED-10 evidence."""
    class_term = await get_deviation_class_term(session, project, class_code)
    now = datetime.now(dt_timezone.utc)

    deviation = Deviation(
        project_id=project.id,
        class_term_id=class_term.id,
        status="confirmed",
        description_en=description_en,
        commitment_id=commitment_id,
        milestone_id=milestone_id,
        confirmed_by=actor_id,
        confirmed_at=now,
    )
    session.add(deviation)
    await session.flush()

    evidence = Evidence(
        deviation_id=deviation.id, channel="manual", sent_at=now, language="en",
        original_text=original_text,
    )
    session.add(evidence)
    await session.flush()

    await record_foresight_audit_event(
        session, project_id=project.id, action="deviation_drafted", actor_id=actor_id,
        deviation_id=deviation.id, detail={"class_code": class_code, "manual": True},
    )
    return deviation


async def confirm_deviation(
    session: AsyncSession,
    *,
    project: Project,
    deviation: Deviation,
    actor_id: uuid.UUID,
    corrections: dict[str, object] | None,
) -> Deviation:
    """FR-DEV-04's "PM confirms or edits", mirroring
    app/api/commitments.py's verify_commitment exactly: a before/after
    `changes` diff, `status` distinctly labels correction-vs-confirmation
    the same way Commitment.verification_state does. `project` is a required
    keyword param (not re-fetched by `deviation.project_id`) because the
    sole caller, confirm_deviation_endpoint, already resolves it via
    Depends(_require_write) — same "already sitting one frame up" posture
    create_manual_deviation's own `project` param already has."""
    changes: dict[str, dict[str, object]] = {}
    for field, new_value in (corrections or {}).items():
        old_value = getattr(deviation, field)
        if new_value != old_value:
            changes[field] = {"before": _jsonable(old_value), "after": _jsonable(new_value)}
            setattr(deviation, field, new_value)

    deviation.status = "confirmed"
    deviation.confirmed_by = actor_id
    deviation.confirmed_at = datetime.now(dt_timezone.utc)
    await session.flush()
    await session.refresh(deviation, attribute_names=["updated_at"])

    await record_foresight_audit_event(
        session, project_id=deviation.project_id,
        action="deviation_confirmed", actor_id=actor_id, deviation_id=deviation.id,
        detail={"changes": changes} if changes else {},
    )
    await dispatch_event(session, project=project, event_type="deviation", deviation=deviation)
    return deviation


async def resolve_deviation(
    session: AsyncSession,
    *,
    project: Project,
    deviation: Deviation,
    actor_id: uuid.UUID,
    resolution_date: datetime,
    resolution_owner: uuid.UUID,
) -> Deviation:
    """FR-DEV-03: resolution date + owner, the two fields this
    requirement names by name. `project` required for the same reason
    confirm_deviation above takes it."""
    deviation.status = "resolved"
    deviation.resolution_date = resolution_date
    deviation.resolution_owner = resolution_owner
    await session.flush()
    await session.refresh(deviation, attribute_names=["updated_at"])

    await record_foresight_audit_event(
        session, project_id=deviation.project_id, action="deviation_resolved",
        actor_id=actor_id, deviation_id=deviation.id,
        detail={"resolution_date": _jsonable(resolution_date), "resolution_owner": str(resolution_owner)},
    )
    await dispatch_event(session, project=project, event_type="deviation", deviation=deviation)
    return deviation
