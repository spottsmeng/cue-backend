from datetime import datetime, timezone as dt_timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.foresight.consequence import milestone_impact_text
from app.foresight.deviation import draft_deviation
from app.foresight.models import Risk
from app.foresight.notification import dispatch_event
from app.foresight.risk import create_or_supersede_risk
from app.foresight.threshold import resolve_threshold
from app.ledger import lifecycle
from app.models import Commitment, Deliverable, Milestone, Project
from app.twin.service import compute_current

# FR-FOR-06 (PRD §6.9): forecasting — "predict milestone-miss likelihood and
# resource bottlenecks from ledger, Twin and historical patterns." There is
# no historical corpus to learn from yet (same situation as FR-TWN-08, see
# backend/PROGRESS.md's M1 notes and CUE-PRD.md §6.8's own paragraph on it)
# — Phase 0 discovery, which would produce one, has not run. This is a
# documented HEURISTIC, not a learned model:
#
#   a milestone at or below the project's configured forecast_slack_days
#   threshold (FR-FOR-07), *combined with* either (a) zero or negative
#   slack on its own, or (b) an open Silence Radar flag on a commitment
#   feeding it, is treated as high miss-likelihood.
#
# Consumes app/twin/service.py's compute_current for slack/critical-path —
# does not recompute CPM independently (Prompt 7's own instruction).
# `base_rate` is always left None on a forecast-sourced Risk: FR-FOR-02's
# base-rate language is Silence Radar's own requirement (app/foresight/
# silence.py's compute_base_rate), and fabricating one here to look more
# sophisticated is exactly what CLAUDE.md's Models table and this heuristic's
# own docstring forbid.
#
# This module also closes FR-LCY-03 (at_risk -> broken on due-time-passed) —
# a plain deterministic sweep, not really "forecasting", but item 6 groups
# both automatic-transition closures into this session and there is no
# other natural home for a time-based commitment sweep.


def _milestone_ids_with_open_silence_flag(
    open_silence_risks: list[Risk], commitments_by_id: dict, deliverables_by_id: dict
) -> set:
    flagged = set()
    for risk in open_silence_risks:
        if risk.commitment_id is None:
            continue
        commitment = commitments_by_id.get(risk.commitment_id)
        if commitment is None or commitment.deliverable_id is None:
            continue
        deliverable = deliverables_by_id.get(commitment.deliverable_id)
        if deliverable is not None and deliverable.milestone_id is not None:
            flagged.add(deliverable.milestone_id)
    return flagged


async def scan_forecast(session: AsyncSession, project: Project) -> list[Risk]:
    """Runs the forecast heuristic for one project. Does not commit —
    caller controls the transaction boundary."""
    twin = await compute_current(session, project.id)
    milestones = (
        await session.execute(select(Milestone).where(Milestone.project_id == project.id))
    ).scalars().all()
    deliverables = (
        await session.execute(select(Deliverable).where(Deliverable.project_id == project.id))
    ).scalars().all()
    deliverables_by_id = {d.id: d for d in deliverables}
    deliverables_by_milestone: dict = {}
    for d in deliverables:
        if d.milestone_id is not None:
            deliverables_by_milestone.setdefault(d.milestone_id, []).append(d.id)

    all_commitments = (
        await session.execute(select(Commitment).where(Commitment.project_id == project.id))
    ).scalars().all()
    commitments_by_id = {c.id: c for c in all_commitments}
    commitments_by_deliverable: dict = {}
    for c in all_commitments:
        if c.deliverable_id is not None:
            commitments_by_deliverable.setdefault(c.deliverable_id, []).append(c)

    open_silence_risks = (
        await session.execute(
            select(Risk).where(Risk.project_id == project.id, Risk.source == "silence", Risk.status == "open")
        )
    ).scalars().all()
    flagged_milestone_ids = _milestone_ids_with_open_silence_flag(
        open_silence_risks, commitments_by_id, deliverables_by_id
    )

    slack_threshold = await resolve_threshold(session, project=project, metric="forecast_slack_days")

    touched: list[Risk] = []
    for milestone in milestones:
        if milestone.is_fixed:
            # A fixed node's earliest/latest are both defined as its own
            # planned_at (app/twin/graph.py's forward/backward pass) — its
            # slack is *definitionally* zero, not a signal of anything at
            # risk. app/twin/graph.py's own _binding_constraint excludes
            # fixed nodes from candidacy for exactly this reason ("the
            # deadline, never the constraint that's binding against it");
            # the forecast heuristic follows the same exclusion, or every
            # fixed milestone in every project would trivially "forecast" a
            # miss on every single scan.
            continue
        node = twin.nodes.get(milestone.id)
        if node is None or node.slack_days is None:
            continue
        has_silence_flag = milestone.id in flagged_milestone_ids
        zero_or_negative_slack = node.slack_days <= 0
        if node.slack_days > slack_threshold or not (zero_or_negative_slack or has_silence_flag):
            continue  # not enough signal for this heuristic to fire

        severity = "critical" if (zero_or_negative_slack and has_silence_flag) else "high"
        consequence = await milestone_impact_text(
            twin, milestone,
            because=(
                f"Forecast heuristic: {node.slack_days:.1f} days of slack"
                + (" (a fed commitment also has an open Silence Radar flag)" if has_silence_flag else "")
            ),
        )
        risk, created = await create_or_supersede_risk(
            session,
            project_id=project.id,
            source="forecast",
            finding_key=f"forecast:{milestone.id}",
            severity=severity,
            downstream_consequence=consequence,
            base_rate=None,  # FR-FOR-06: no historical corpus — see module docstring
            milestone_id=milestone.id,
            detail={
                "slack_days": round(node.slack_days, 2),
                "slack_threshold_days": slack_threshold,
                "silence_flag": has_silence_flag,
                "is_critical": node.is_critical,
            },
        )
        touched.append(risk)

        if created:
            await dispatch_event(session, project=project, event_type="risk", risk=risk)
            # FR-LCY-02: forecast breach is the third named automatic
            # trigger for committed -> at_risk, applied to every still-
            # committed commitment feeding this milestone.
            for deliverable_id in deliverables_by_milestone.get(milestone.id, []):
                for commitment in commitments_by_deliverable.get(deliverable_id, []):
                    if commitment.state == "committed":
                        await lifecycle.apply_automatic_transition(
                            session,
                            project_id=project.id,
                            commitment=commitment,
                            to_state="at_risk",
                            trigger="forecast",
                            detail={"risk_id": str(risk.id), "milestone_id": str(milestone.id)},
                        )

    return touched


async def scan_overdue_commitments(session: AsyncSession, project: Project) -> list[Commitment]:
    """FR-LCY-03: at_risk -> broken once due_at passes with no delivery
    evidence. A broken commitment is itself a qualifying FR-DEV-04 event —
    auto-drafts a Deviation (class 'broken_commitment') alongside the
    transition, not left for a human to notice and log separately."""
    now = datetime.now(dt_timezone.utc)
    commitments = (
        await session.execute(
            select(Commitment).where(
                Commitment.project_id == project.id,
                Commitment.state == "at_risk",
                Commitment.due_at.is_not(None),
                Commitment.due_at < now,
            )
        )
    ).scalars().all()

    transitioned: list[Commitment] = []
    for commitment in commitments:
        await lifecycle.apply_automatic_transition(
            session,
            project_id=project.id,
            commitment=commitment,
            to_state="broken",
            trigger="due_time_passed",
            detail={"due_at": commitment.due_at.isoformat()},
        )
        await draft_deviation(
            session,
            project=project,
            class_code="broken_commitment",
            description_en=f"Commitment broken on due-time-passed: {commitment.deliverable_en}",
            commitment_id=commitment.id,
            evidence_text=(
                f"Automatic FR-LCY-03 transition: due_at {commitment.due_at.isoformat()} passed "
                "with no delivery evidence."
            ),
        )
        await dispatch_event(session, project=project, event_type="commitment", commitment=commitment)
        transitioned.append(commitment)

    return transitioned
