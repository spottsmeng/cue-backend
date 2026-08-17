"""FR-ASK-07: Successor Brief — 'a full handover context pack for an
incoming PM in one action.' Structurally the same composer-pattern
app/reports/composer.py (Prompt 8) established: one compose_successor_brief()
calling one async per-section helper each, reading through the same
RLS-scoped session passed in, never a second connection or a parallel query
path — reused rather than designing a second composer shape from scratch,
per Prompt 9's own instruction.
"""

import uuid
from datetime import datetime, timezone as dt_timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ask.schema import DeviationResolutionRow, KeyDocumentRow, SuccessorBriefOut, VendorContactRow
from app.documents.models import Document
from app.foresight.models import Deviation, Risk
from app.models import AuditLog, Commitment, Party, Project
from app.reports.composer import _OPEN_RISK_STATUSES, _commitment_summary
from app.reports.schema import DecisionLogRow, ReportProvenance, RiskLogRow

_OPEN_COMMITMENT_STATES = ("proposed", "committed", "at_risk", "renegotiated")


async def _open_commitments(session: AsyncSession, project: Project) -> list:
    rows = (
        await session.execute(
            select(Commitment)
            .where(Commitment.project_id == project.id, Commitment.state.in_(_OPEN_COMMITMENT_STATES))
            .order_by(Commitment.due_at.asc().nulls_last())
        )
    ).scalars().all()
    return [_commitment_summary(c) for c in rows]


async def _decision_history(session: AsyncSession, project: Project) -> list[DecisionLogRow]:
    rows = (
        await session.execute(
            select(AuditLog)
            .where(AuditLog.project_id == project.id, AuditLog.action != "created")
            .order_by(AuditLog.occurred_at.desc())
        )
    ).scalars().all()
    return [
        DecisionLogRow(
            audit_log_id=a.id, commitment_id=a.commitment_id, action=a.action, actor_id=a.actor_id,
            occurred_at=a.occurred_at, from_state=a.from_state, to_state=a.to_state, detail=a.detail,
            provenance=ReportProvenance(source_type="audit_log", source_id=a.id),
        )
        for a in rows
    ]


async def _open_risks(session: AsyncSession, project: Project) -> list[RiskLogRow]:
    rows = (
        await session.execute(
            select(Risk)
            .where(Risk.project_id == project.id, Risk.status.in_(_OPEN_RISK_STATUSES))
            .order_by(Risk.severity.desc(), Risk.created_at.desc())
        )
    ).scalars().all()
    return [
        RiskLogRow(
            risk_id=r.id, source=r.source, severity=r.severity, status=r.status,
            downstream_consequence=r.downstream_consequence, base_rate=r.base_rate,
            provenance=ReportProvenance(source_type="risk", source_id=r.id),
        )
        for r in rows
    ]


async def _key_documents(session: AsyncSession, project: Project) -> list[KeyDocumentRow]:
    rows = (
        await session.execute(
            select(Document).where(Document.project_id == project.id).order_by(Document.created_at.desc())
        )
    ).scalars().all()
    return [
        KeyDocumentRow(
            document_id=d.id, name=d.name, current_version_id=d.current_version_id,
            approved=d.current_version_id is not None,
            provenance=ReportProvenance(source_type="document", source_id=d.id, label=d.name),
        )
        for d in rows
    ]


async def _vendor_contacts(session: AsyncSession, project: Project) -> list[VendorContactRow]:
    """One row per vendor Party this project has ever committed with — Party
    is org-scoped (app/models/party.py), so "this project's vendor
    contacts" is the set reachable via its own Commitments, same join
    app/reports/composer.py's _compose_vendor_status already uses."""
    rows = (
        await session.execute(
            select(Commitment, Party)
            .join(Party, Party.id == Commitment.party_id)
            .where(Commitment.project_id == project.id, Party.type == "vendor_org")
        )
    ).all()
    by_party: dict[uuid.UUID, tuple[Party, int]] = {}
    for commitment, party in rows:
        existing_party, count = by_party.get(party.id, (party, 0))
        by_party[party.id] = (existing_party, count + (1 if commitment.state in _OPEN_COMMITMENT_STATES else 0))
    return [
        VendorContactRow(party_id=party.id, display_name=party.display_name, open_commitment_count=count)
        for party, count in sorted(by_party.values(), key=lambda pc: pc[0].display_name)
    ]


async def _deviations_and_resolutions(session: AsyncSession, project: Project) -> list[DeviationResolutionRow]:
    """Every deviation, not just open ones — a handover needs to see what
    was already resolved and how, not only what's still outstanding
    (app/reports/composer.py's own risk_and_issues section, by contrast,
    deliberately scopes to open-only, since that section is a *current*
    issues log, not a history)."""
    rows = (
        await session.execute(
            select(Deviation).where(Deviation.project_id == project.id).order_by(Deviation.created_at.desc())
        )
    ).scalars().all()
    return [
        DeviationResolutionRow(
            deviation_id=d.id, status=d.status, description_en=d.description_en,
            resolution_date=d.resolution_date, resolution_owner=d.resolution_owner,
            provenance=ReportProvenance(source_type="deviation", source_id=d.id),
        )
        for d in rows
    ]


async def compose_successor_brief(session: AsyncSession, project: Project) -> SuccessorBriefOut:
    return SuccessorBriefOut(
        project_id=project.id,
        generated_at=datetime.now(dt_timezone.utc),
        open_commitments=await _open_commitments(session, project),
        decision_history=await _decision_history(session, project),
        risks=await _open_risks(session, project),
        key_documents=await _key_documents(session, project),
        vendor_contacts=await _vendor_contacts(session, project),
        deviations_and_resolutions=await _deviations_and_resolutions(session, project),
    )
