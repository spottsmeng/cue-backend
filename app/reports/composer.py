"""FR-RPT-01/02/03/04/06: assembles the Living WIP report's seven sections,
fresh from the ledger/Twin/Foresight/Documents/audit tables on every call —
no cache, no "current report" table (see app/reports/models.py's module
docstring for why). Every scalar figure is wrapped in a ReportField
(app/reports/schema.py) carrying its provenance and verification state, per
§8.4's "the report composer refuses to render a field without a resolvable
provenance link" — this module is that refusal, expressed as
`available=False` + `unavailable_reason` rather than a fabricated value or a
silently missing field.

Deliberately reads the same tables app/api/commitments.py, app/api/
deviations.py etc. already RLS/role-scope correctly (via the RLS-enforced
session passed in — this module never opens a second connection), not a
parallel query path, per Prompt 8's own instruction. Vendor status,
milestone tracker and risk/issues data could in principle be fetched by
calling those routers' own endpoint functions directly, but a plain SELECT
against the already-scoped session is simpler and exactly what those routers
themselves do internally — there is no privileged path here a REST call
would have added.
"""

import uuid
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.documents.models import Document, DocumentVersion, SpecClaim
from app.documents.storage import StorageBackend
from app.foresight.models import Deviation, Risk
from app.models import (
    AuditLog,
    Budget,
    Commitment,
    Deliverable,
    Milestone,
    OntologyTerm,
    Party,
    Project,
)
from app.reports.schema import (
    BudgetSummarySection,
    CommitmentSummary,
    DecisionAndApprovalLogSection,
    DecisionLogRow,
    DeviationLogRow,
    LivingWipReportOut,
    MilestoneTrackerRow,
    NextStepMilestoneRow,
    NextStepsSection,
    OutstandingApprovalRow,
    ProjectOverviewSection,
    ProjectOverviewVisualReference,
    ReportField,
    ReportProvenance,
    RiskAndIssuesSection,
    RiskLogRow,
    VendorStatusRow,
    VendorStatusSection,
)
from app.twin.service import compute_current

# FR-RPT-02's vendor status summary degrades gracefully when the Vendor
# Reliability Graph (M7) doesn't exist yet, per Prompt 8's own explicit
# instruction: "check backend/PROGRESS.md for M7's status and branch
# accordingly, don't hard-fail." M7 (Prompt 10) added app/parties/ and this
# import now succeeds — real metrics resolve below, per that session's own
# note that no change would be needed here beyond this import starting to
# work. The try/except stays (rather than becoming an unconditional import)
# so this module still degrades honestly if a future refactor ever removes
# app/parties/ again, same posture the rest of this codebase takes for
# every other optional cross-milestone dependency (app/parties/compute.py's
# own Deviation-availability guard, for one).
try:
    from app.parties.reliability import get_reliability_metrics

    _VRG_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only in a build without M7
    _VRG_AVAILABLE = False

_COMMITTED_STATES = ("committed", "at_risk", "delivered")
_OPEN_COMMITMENT_STATES = ("proposed", "committed", "at_risk", "renegotiated")
_OPEN_RISK_STATUSES = ("open", "acknowledged")
_OPEN_DEVIATION_STATUSES = ("auto_drafted", "confirmed")


def _num(value: Decimal | float | None) -> float | None:
    """Numeric(12, 2) columns (Commitment.amount, Budget.approved_amount)
    come back as Decimal at runtime despite their `float` type hint — same
    conversion app/api/commitments.py's _jsonable already performs for the
    audit trail; ReportField.value needs the same treatment before it can be
    serialised."""
    return float(value) if value is not None else None


def _commitment_provenance(commitment: Commitment) -> ReportProvenance:
    return ReportProvenance(
        source_type="commitment", source_id=commitment.id, label=commitment.deliverable_en
    )


def _commitment_summary(commitment: Commitment) -> CommitmentSummary:
    return CommitmentSummary(
        commitment_id=commitment.id,
        deliverable_en=commitment.deliverable_en,
        state=commitment.state,
        due_at=commitment.due_at,
        amount=_num(commitment.amount),
        currency=commitment.currency,
        verification_state=commitment.verification_state,
        provenance=_commitment_provenance(commitment),
    )


async def compute_budget_summary(
    session: AsyncSession, project: Project
) -> tuple[BudgetSummarySection, list[Commitment]]:
    """§6.10's own paragraph, verbatim — the single implementation of the
    budget summary's four-field grounding rule, reused by both
    compose_report (below) and app/reports/service.py's export verification
    gate (FR-RPT-06), so the report a Producer sees and the gate that blocks
    export are never computed two different ways.

    Returns the section plus every Commitment that fed committed_spend or
    outstanding_payments and currently sits at `pending_verification` — the
    export gate's own "which commitment(s) are blocking it" list.
    """
    budget = (
        await session.execute(
            select(Budget).where(Budget.project_id == project.id, Budget.is_current.is_(True))
        )
    ).scalar_one_or_none()

    if budget is not None:
        approved_field = ReportField(
            value=_num(budget.approved_amount),
            available=True,
            verification_state="not_applicable",
            provenance=[ReportProvenance(source_type="budget", source_id=budget.id)],
        )
    else:
        approved_field = ReportField(
            value=None,
            available=False,
            unavailable_reason="no budget baseline recorded for this project yet (FR-ADM-11)",
        )

    committed_commitments = list(
        (
            await session.execute(
                select(Commitment).where(
                    Commitment.project_id == project.id,
                    Commitment.state.in_(_COMMITTED_STATES),
                    Commitment.amount.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    # §6.10: "outstanding payments as the sum of commitments where
    # payment_status is not paid" — no state filter in the PRD's own
    # wording, so this deliberately sums across every state, including a
    # still-`proposed` or even `withdrawn` commitment whose payment_status
    # was never advanced past NULL. CLAUDE.md: "don't compute any of them
    # differently than that paragraph specifies" — not narrowed here on the
    # assumption that's what was "really" meant.
    outstanding_commitments = list(
        (
            await session.execute(
                select(Commitment).where(
                    Commitment.project_id == project.id,
                    Commitment.payment_status.is_distinct_from("paid"),
                    Commitment.amount.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )

    def _aggregate_field(commitments: list[Commitment]) -> ReportField:
        total = sum((c.amount for c in commitments), start=Decimal("0"))
        states = {c.verification_state for c in commitments}
        if not states:
            verification_state = "not_applicable"
        elif "pending_verification" in states:
            verification_state = "pending_verification"
        elif states <= {"human_verified", "human_corrected"}:
            verification_state = "human_verified"
        else:
            verification_state = "auto"
        return ReportField(
            value=_num(total),
            available=True,  # a sum over a possibly-empty set is always a resolvable 0, not missing data
            verification_state=verification_state,
            provenance=[_commitment_provenance(c) for c in commitments],
        )

    committed_field = _aggregate_field(committed_commitments)
    outstanding_field = _aggregate_field(outstanding_commitments)

    if approved_field.available:
        variance_field = ReportField(
            value=approved_field.value - committed_field.value,
            available=True,
            verification_state=committed_field.verification_state,
            provenance=[*approved_field.provenance, *committed_field.provenance],
        )
    else:
        variance_field = ReportField(
            value=None,
            available=False,
            unavailable_reason="variance requires an approved budget baseline, which is not available",
        )

    blocking_by_id: dict[uuid.UUID, Commitment] = {
        c.id: c
        for c in (*committed_commitments, *outstanding_commitments)
        if c.verification_state == "pending_verification"
    }

    section = BudgetSummarySection(
        currency=budget.currency if budget is not None else None,
        approved_budget=approved_field,
        committed_spend=committed_field,
        outstanding_payments=outstanding_field,
        variance=variance_field,
    )
    return section, list(blocking_by_id.values())


def _milestone_status(
    planned_at: datetime | None, actual_at: datetime | None, is_critical: bool, slack_days: float | None
) -> str:
    if actual_at is not None:
        return "completed"
    if planned_at is None:
        return "no_date"
    if slack_days is not None and slack_days < 0:
        return "critical"
    if is_critical:
        return "at_risk"
    return "on_track"


async def _compose_milestone_tracker(session: AsyncSession, project: Project) -> list[MilestoneTrackerRow]:
    milestones = (
        (
            await session.execute(
                select(Milestone)
                .where(Milestone.project_id == project.id)
                .order_by(Milestone.planned_at.asc().nulls_last())
            )
        )
        .scalars()
        .all()
    )
    twin = await compute_current(session, project.id)
    rows = []
    for m in milestones:
        node = twin.nodes.get(m.id)
        is_critical = node.is_critical if node else False
        slack_days = node.slack_days if node else None
        rows.append(
            MilestoneTrackerRow(
                milestone_id=m.id,
                name=m.name,
                planned_at=m.planned_at,
                actual_at=m.actual_at,
                status=_milestone_status(m.planned_at, m.actual_at, is_critical, slack_days),
                is_critical=is_critical,
                slack_days=slack_days,
                provenance=ReportProvenance(source_type="milestone", source_id=m.id),
            )
        )
    return rows


async def _compose_vendor_status(session: AsyncSession, project: Project) -> VendorStatusSection:
    rows = (
        await session.execute(
            select(Commitment, Party)
            .join(Party, Party.id == Commitment.party_id)
            .where(Commitment.project_id == project.id, Party.type == "vendor_org")
        )
    ).all()

    by_party: dict[uuid.UUID, tuple[Party, list[Commitment]]] = {}
    for commitment, party in rows:
        _, commitments = by_party.setdefault(party.id, (party, []))
        commitments.append(commitment)

    vendor_rows = []
    for party, commitments in by_party.values():
        if _VRG_AVAILABLE:
            # FR-VRG-01's on-time commitment rate is this section's
            # headline "reliability" figure — the single metric closest to
            # what "reliability" plainly means to a Producer skimming this
            # report; the full metric set (median response time, revision
            # churn, price drift, deviation frequency), segmented, lives at
            # §11.2's own /parties/{id}/reliability, not duplicated here.
            metrics = await get_reliability_metrics(session, party.id)
            on_time = metrics.get("on_time_rate")
            if on_time is None:
                reliability = ReportField(
                    value=None, available=False,
                    unavailable_reason="no on-time-rate metric has been computed for this vendor yet",
                )
            else:
                reliability = ReportField(
                    value=on_time.value,
                    available=on_time.available,
                    unavailable_reason=on_time.unavailable_reason,
                    provenance=[
                        ReportProvenance(
                            source_type="vendor_metric", source_id=None, label="on_time_rate"
                        )
                    ],
                )
        else:
            reliability = ReportField(
                value=None,
                available=False,
                unavailable_reason=(
                    "Vendor Reliability Graph (M7, backend/PROGRESS.md) is not yet implemented"
                ),
            )
        vendor_rows.append(
            VendorStatusRow(
                party_id=party.id,
                party_name=party.display_name,
                outstanding_actions=[
                    _commitment_summary(c) for c in commitments if c.state in _OPEN_COMMITMENT_STATES
                ],
                confirmed_deliverables=[
                    _commitment_summary(c) for c in commitments if c.state == "delivered"
                ],
                reliability=reliability,
            )
        )
    vendor_rows.sort(key=lambda r: r.party_name)
    return VendorStatusSection(vendors=vendor_rows, reliability_data_available=_VRG_AVAILABLE)


async def _compose_risk_and_issues(session: AsyncSession, project: Project) -> RiskAndIssuesSection:
    risks = (
        await session.execute(
            select(Risk)
            .where(Risk.project_id == project.id, Risk.status.in_(_OPEN_RISK_STATUSES))
            .order_by(Risk.severity.desc(), Risk.created_at.desc())
        )
    ).scalars().all()
    deviations = (
        await session.execute(
            select(Deviation)
            .where(Deviation.project_id == project.id, Deviation.status.in_(_OPEN_DEVIATION_STATUSES))
            .order_by(Deviation.created_at.desc())
        )
    ).scalars().all()

    class_term_ids = {d.class_term_id for d in deviations}
    code_by_term_id: dict[uuid.UUID, str] = {}
    if class_term_ids:
        terms = (
            await session.execute(select(OntologyTerm).where(OntologyTerm.id.in_(class_term_ids)))
        ).scalars().all()
        code_by_term_id = {t.id: t.code for t in terms}

    return RiskAndIssuesSection(
        risks=[
            RiskLogRow(
                risk_id=r.id,
                source=r.source,
                severity=r.severity,
                status=r.status,
                downstream_consequence=r.downstream_consequence,
                base_rate=r.base_rate,
                provenance=ReportProvenance(source_type="risk", source_id=r.id),
            )
            for r in risks
        ],
        deviations=[
            DeviationLogRow(
                deviation_id=d.id,
                class_code=code_by_term_id.get(d.class_term_id),
                status=d.status,
                description_en=d.description_en,
                provenance=ReportProvenance(source_type="deviation", source_id=d.id),
            )
            for d in deviations
        ],
    )


async def _compose_decision_and_approval_log(
    session: AsyncSession, project: Project
) -> DecisionAndApprovalLogSection:
    audit_rows = (
        await session.execute(
            select(AuditLog)
            .where(AuditLog.project_id == project.id, AuditLog.action != "created")
            .order_by(AuditLog.occurred_at.desc())
        )
    ).scalars().all()
    pending = (
        await session.execute(
            select(Commitment).where(
                Commitment.project_id == project.id,
                Commitment.verification_state == "pending_verification",
            )
        )
    ).scalars().all()

    return DecisionAndApprovalLogSection(
        decisions=[
            DecisionLogRow(
                audit_log_id=a.id,
                commitment_id=a.commitment_id,
                action=a.action,
                actor_id=a.actor_id,
                occurred_at=a.occurred_at,
                from_state=a.from_state,
                to_state=a.to_state,
                detail=a.detail,
                provenance=ReportProvenance(source_type="audit_log", source_id=a.id),
            )
            for a in audit_rows
        ],
        outstanding_approvals=[
            OutstandingApprovalRow(
                commitment_id=c.id,
                deliverable_en=c.deliverable_en,
                due_at=c.due_at,
                provenance=_commitment_provenance(c),
            )
            for c in pending
        ],
    )


async def _compose_next_steps(session: AsyncSession, project: Project) -> NextStepsSection:
    open_commitments = (
        await session.execute(
            select(Commitment)
            .where(Commitment.project_id == project.id, Commitment.state.in_(_OPEN_COMMITMENT_STATES))
            .order_by(Commitment.due_at.asc().nulls_last())
        )
    ).scalars().all()
    open_milestones = (
        await session.execute(
            select(Milestone)
            .where(Milestone.project_id == project.id, Milestone.actual_at.is_(None))
            .order_by(Milestone.planned_at.asc().nulls_last())
        )
    ).scalars().all()

    return NextStepsSection(
        open_commitments=[_commitment_summary(c) for c in open_commitments],
        open_milestones=[
            NextStepMilestoneRow(
                milestone_id=m.id,
                name=m.name,
                planned_at=m.planned_at,
                provenance=ReportProvenance(source_type="milestone", source_id=m.id),
            )
            for m in open_milestones
        ],
    )


async def _compose_project_overview(
    session: AsyncSession, project: Project, storage: StorageBackend
) -> ProjectOverviewSection:
    def _project_field(value: object) -> ReportField:
        return ReportField(
            value=value,
            available=value is not None,
            unavailable_reason=None if value is not None else "not set on this project",
            provenance=[ReportProvenance(source_type="project", source_id=project.id)],
        )

    # FR-RPT-03: resolved via the only FK path Deliverable and Document
    # actually share today — SpecClaim.deliverable_id -> DocumentVersion ->
    # Document (Document itself carries no direct deliverable_id). See
    # ProjectOverviewVisualReference's own docstring.
    link_rows = (
        await session.execute(
            select(SpecClaim.deliverable_id, Document, Deliverable.name)
            .join(DocumentVersion, DocumentVersion.id == SpecClaim.document_version_id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .join(Deliverable, Deliverable.id == SpecClaim.deliverable_id)
            .where(Document.project_id == project.id, SpecClaim.deliverable_id.is_not(None))
            .distinct()
        )
    ).all()

    total_deliverables = (
        await session.execute(select(Deliverable.id).where(Deliverable.project_id == project.id))
    ).scalars().all()

    visual_refs: list[ProjectOverviewVisualReference] = []
    deliverables_with_reference: set[uuid.UUID] = set()
    for deliverable_id, document, deliverable_name in link_rows:
        deliverables_with_reference.add(deliverable_id)
        current_version = None
        if document.current_version_id is not None:
            current_version = (
                await session.execute(
                    select(DocumentVersion).where(DocumentVersion.id == document.current_version_id)
                )
            ).scalar_one_or_none()

        if current_version is not None and current_version.approved_at is not None:
            visual_refs.append(
                ProjectOverviewVisualReference(
                    deliverable_id=deliverable_id,
                    deliverable_name=deliverable_name,
                    document_id=document.id,
                    document_version_id=current_version.id,
                    image_url=storage.signed_url(current_version.storage_ref),
                    available=True,
                    unavailable_reason=None,
                    provenance=[ReportProvenance(source_type="document_version", source_id=current_version.id)],
                )
            )
        else:
            reason = (
                "current version not yet approved"
                if current_version is not None
                else "document has no current version"
            )
            visual_refs.append(
                ProjectOverviewVisualReference(
                    deliverable_id=deliverable_id,
                    deliverable_name=deliverable_name,
                    document_id=document.id,
                    document_version_id=None,
                    image_url=None,
                    available=False,
                    unavailable_reason=reason,
                    provenance=[ReportProvenance(source_type="document", source_id=document.id)],
                )
            )

    return ProjectOverviewSection(
        project_name=_project_field(project.name),
        client_name=_project_field(project.client_name),
        venue=_project_field(project.venue),
        event_start=_project_field(project.event_start.isoformat() if project.event_start else None),
        event_end=_project_field(project.event_end.isoformat() if project.event_end else None),
        current_phase=ReportField(
            value=None,
            available=False,
            unavailable_reason=(
                "no phase-tracking field exists at the project level in this schema — "
                "Document.phase_term_id tags individual documents, not the project as a whole "
                "(backend/PROGRESS.md's M3 notes)"
            ),
        ),
        visual_references=visual_refs,
        deliverables_without_visual_reference=len(
            [d for d in total_deliverables if d not in deliverables_with_reference]
        ),
    )


async def compose_report(
    session: AsyncSession, project: Project, storage: StorageBackend
) -> LivingWipReportOut:
    """FR-RPT-01/02: the current Living WIP report, recomputed fresh — no
    caching this session (Prompt 8's own instruction), so "current" and
    "just generated" are always the same thing."""
    budget_summary, _blocking = await compute_budget_summary(session, project)
    return LivingWipReportOut(
        project_id=project.id,
        generated_at=datetime.now(dt_timezone.utc),
        project_overview=await _compose_project_overview(session, project, storage),
        milestone_tracker=await _compose_milestone_tracker(session, project),
        vendor_status=await _compose_vendor_status(session, project),
        budget_summary=budget_summary,
        risk_and_issues=await _compose_risk_and_issues(session, project),
        decision_and_approval_log=await _compose_decision_and_approval_log(session, project),
        next_steps=await _compose_next_steps(session, project),
    )
