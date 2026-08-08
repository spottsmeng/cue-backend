"""FR-ASK-04/05: assistant-shaped grounded summaries — project status,
vendor status, period digest, decision history, and (FR-ASK-05, its own
explicitly separate variant) outstanding actions grouped by owner and by
due window. Deliberately distinct from GET /commitments's raw state/party/
due-window *filters* (Prompt 9's own instruction) — every figure here is
either a ReportField (app/reports/schema.py's provenance-carrying wrapper,
reused rather than re-invented, per "grounded the same way") or one of that
same module's row types (CommitmentSummary, DecisionLogRow), not a parallel
set of types for what is the same underlying ledger data shaped differently.
"""

import uuid
from datetime import datetime, timezone as dt_timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ask.schema import (
    AskVendorStatusSummary,
    DecisionHistorySummary,
    OutstandingActionsByOwner,
    OutstandingActionsByWindow,
    OutstandingActionsSummary,
    PeriodDigestSummary,
    ProjectStatusSummary,
)
from app.foresight.models import Deviation, Risk
from app.models import AuditLog, Commitment, Milestone, Party, Project
from app.reports.composer import _compose_vendor_status  # reuse directly — no second implementation
from app.reports.schema import CommitmentSummary, DecisionLogRow, ReportField, ReportProvenance

_OPEN_COMMITMENT_STATES = ("proposed", "committed", "at_risk", "renegotiated")
_OPEN_RISK_STATUSES = ("open", "acknowledged")
_OPEN_DEVIATION_STATUSES = ("auto_drafted", "confirmed")


def _num(value) -> float | None:
    return float(value) if value is not None else None


def _commitment_summary(c: Commitment) -> CommitmentSummary:
    return CommitmentSummary(
        commitment_id=c.id, deliverable_en=c.deliverable_en, state=c.state, due_at=c.due_at,
        amount=_num(c.amount), currency=c.currency, verification_state=c.verification_state,
        provenance=ReportProvenance(source_type="commitment", source_id=c.id, label=c.deliverable_en),
    )


async def compose_project_status(session: AsyncSession, project: Project) -> ProjectStatusSummary:
    open_commitments = (
        await session.execute(
            select(Commitment).where(
                Commitment.project_id == project.id, Commitment.state.in_(_OPEN_COMMITMENT_STATES)
            )
        )
    ).scalars().all()
    at_risk = [c for c in open_commitments if c.state == "at_risk"]
    open_risk_ids = (
        await session.execute(
            select(Risk.id).where(Risk.project_id == project.id, Risk.status.in_(_OPEN_RISK_STATUSES))
        )
    ).scalars().all()
    open_deviation_ids = (
        await session.execute(
            select(Deviation.id).where(
                Deviation.project_id == project.id, Deviation.status.in_(_OPEN_DEVIATION_STATUSES)
            )
        )
    ).scalars().all()
    next_milestone = (
        await session.execute(
            select(Milestone)
            .where(
                Milestone.project_id == project.id,
                Milestone.actual_at.is_(None),
                Milestone.planned_at.is_not(None),
            )
            .order_by(Milestone.planned_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if next_milestone is not None:
        milestone_provenance = [ReportProvenance(source_type="milestone", source_id=next_milestone.id)]
        next_milestone_name = ReportField(
            value=next_milestone.name, available=True, provenance=milestone_provenance
        )
        next_milestone_planned_at = ReportField(
            value=next_milestone.planned_at.isoformat(), available=True, provenance=milestone_provenance
        )
    else:
        next_milestone_name = ReportField(
            value=None, available=False, unavailable_reason="no upcoming milestone with a planned date"
        )
        next_milestone_planned_at = ReportField(
            value=None, available=False, unavailable_reason="no upcoming milestone with a planned date"
        )

    return ProjectStatusSummary(
        project_name=ReportField(
            value=project.name, available=True,
            provenance=[ReportProvenance(source_type="project", source_id=project.id)],
        ),
        open_commitment_count=ReportField(
            value=len(open_commitments), available=True,
            provenance=[ReportProvenance(source_type="commitment", source_id=c.id) for c in open_commitments],
        ),
        at_risk_commitment_count=ReportField(
            value=len(at_risk), available=True,
            provenance=[ReportProvenance(source_type="commitment", source_id=c.id) for c in at_risk],
        ),
        open_risk_count=ReportField(
            value=len(open_risk_ids), available=True,
            provenance=[ReportProvenance(source_type="risk", source_id=i) for i in open_risk_ids],
        ),
        open_deviation_count=ReportField(
            value=len(open_deviation_ids), available=True,
            provenance=[ReportProvenance(source_type="deviation", source_id=i) for i in open_deviation_ids],
        ),
        next_milestone_name=next_milestone_name,
        next_milestone_planned_at=next_milestone_planned_at,
    )


async def compose_vendor_status(session: AsyncSession, project: Project) -> AskVendorStatusSummary:
    section = await _compose_vendor_status(session, project)
    return AskVendorStatusSummary(
        vendors=section.vendors, reliability_data_available=section.reliability_data_available
    )


def _decision_log_row(a: AuditLog) -> DecisionLogRow:
    return DecisionLogRow(
        audit_log_id=a.id, commitment_id=a.commitment_id, action=a.action, actor_id=a.actor_id,
        occurred_at=a.occurred_at, from_state=a.from_state, to_state=a.to_state,
        provenance=ReportProvenance(source_type="audit_log", source_id=a.id),
    )


async def compose_period_digest(
    session: AsyncSession, project: Project, period_start: datetime, period_end: datetime
) -> PeriodDigestSummary:
    created = (
        await session.execute(
            select(Commitment).where(
                Commitment.project_id == project.id,
                Commitment.created_at >= period_start, Commitment.created_at <= period_end,
            )
        )
    ).scalars().all()
    resolved = (
        await session.execute(
            select(Commitment).where(
                Commitment.project_id == project.id,
                Commitment.state.in_(("delivered", "broken")),
                Commitment.updated_at >= period_start, Commitment.updated_at <= period_end,
            )
        )
    ).scalars().all()
    decisions = (
        await session.execute(
            select(AuditLog)
            .where(
                AuditLog.project_id == project.id, AuditLog.action != "created",
                AuditLog.occurred_at >= period_start, AuditLog.occurred_at <= period_end,
            )
            .order_by(AuditLog.occurred_at.desc())
        )
    ).scalars().all()
    return PeriodDigestSummary(
        period_start=period_start, period_end=period_end,
        commitments_created=[_commitment_summary(c) for c in created],
        commitments_resolved=[_commitment_summary(c) for c in resolved],
        decisions=[_decision_log_row(a) for a in decisions],
    )


async def compose_decision_history(session: AsyncSession, project: Project) -> DecisionHistorySummary:
    rows = (
        await session.execute(
            select(AuditLog)
            .where(AuditLog.project_id == project.id, AuditLog.action != "created")
            .order_by(AuditLog.occurred_at.desc())
        )
    ).scalars().all()
    return DecisionHistorySummary(decisions=[_decision_log_row(a) for a in rows])


def _due_window(due_at: datetime | None, now: datetime) -> str:
    if due_at is None:
        return "no_due_date"
    if due_at < now:
        return "overdue"
    delta_days = (due_at - now).days
    if delta_days <= 7:
        return "due_within_7_days"
    if delta_days <= 30:
        return "due_within_30_days"
    return "due_later"


async def compose_outstanding_actions(session: AsyncSession, project: Project) -> OutstandingActionsSummary:
    rows = (
        await session.execute(
            select(Commitment, Party)
            .join(Party, Party.id == Commitment.party_id)
            .where(Commitment.project_id == project.id, Commitment.state.in_(_OPEN_COMMITMENT_STATES))
        )
    ).all()

    by_party: dict[uuid.UUID, tuple[Party, list[Commitment]]] = {}
    by_window: dict[str, list[Commitment]] = {}
    now = datetime.now(dt_timezone.utc)
    for commitment, party in rows:
        _, bucket = by_party.setdefault(party.id, (party, []))
        bucket.append(commitment)
        by_window.setdefault(_due_window(commitment.due_at, now), []).append(commitment)

    owner_rows = [
        OutstandingActionsByOwner(
            party_id=party.id, party_name=party.display_name,
            commitments=[_commitment_summary(c) for c in commitments],
        )
        for party, commitments in sorted(by_party.values(), key=lambda pc: pc[0].display_name)
    ]
    window_order = ["overdue", "due_within_7_days", "due_within_30_days", "due_later", "no_due_date"]
    window_rows = [
        OutstandingActionsByWindow(window=w, commitments=[_commitment_summary(c) for c in by_window[w]])
        for w in window_order
        if w in by_window
    ]
    return OutstandingActionsSummary(by_owner=owner_rows, by_due_window=window_rows)
