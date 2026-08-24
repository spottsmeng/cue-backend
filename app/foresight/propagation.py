import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.foresight.deviation import UnknownDeviationClass, draft_deviation
from app.foresight.models import Risk
from app.foresight.notification import dispatch_event
from app.foresight.risk import create_or_supersede_risk
from app.models import Milestone, Project
from app.twin.graph import AffectedNode, DependencyEdge, MilestoneNode, PropagationResult, compute_twin, propagate

# The brief's own AV-delay example: "a two-hour delay ... should prompt the
# system to assess and surface how that delay affects dependent tasks,
# vendor sequencing and overall event readiness" — not just flag the one
# thing that moved. app/twin/graph.py's propagate() already computes exactly
# that (a real downstream-closure walk over the dependency graph), but was
# previously reachable only from the PM-triggered /twin/propagate what-if
# endpoint. This module is what runs it automatically, the moment a
# milestone's planned date is actually rescheduled (app/api/milestones.py's
# update_milestone), and feeds the result through the same Risk ->
# Deviation -> Notification pipeline every other detector in this package
# (silence.py, forecast.py, contradiction.py) already uses.


def _format_consequence(
    milestone: Milestone, names: dict[uuid.UUID, str], result: PropagationResult, downstream: list[AffectedNode]
) -> str:
    parts = []
    for a in downstream:
        name = names.get(a.milestone_id, str(a.milestone_id))
        if a.propagation_stopped:
            parts.append(
                f"{name!r} is fixed and holds its date, absorbing "
                f"{a.consumed_slack_days:.1f} days of buffer — propagation stops there"
                if a.consumed_slack_days is not None
                else f"{name!r} is fixed and holds its date — propagation stops there"
            )
        else:
            parts.append(
                f"{name!r} shifts to {a.new_earliest:%d %b %Y %H:%M}"
                + (f" ({a.consumed_slack_days:.1f} days of slack consumed)" if a.consumed_slack_days is not None else "")
            )
    return (
        f"{milestone.name!r} moved to {result.new_date:%d %b %Y %H:%M}, "
        f"pushing {len(downstream)} downstream milestone(s): " + "; ".join(parts)
    )


def _now_critical(a: AffectedNode, baseline_slack: dict[uuid.UUID, float | None]) -> bool:
    if a.propagation_stopped or a.consumed_slack_days is None:
        return False
    slack_before = baseline_slack.get(a.milestone_id)
    if slack_before is None:
        return False
    return (slack_before - a.consumed_slack_days) <= 0


async def record_milestone_delay(
    session: AsyncSession,
    *,
    project: Project,
    milestone: Milestone,
    baseline_nodes: list[MilestoneNode],
    baseline_edges: list[DependencyEdge],
    new_date: datetime,
) -> Risk | None:
    """Called from update_milestone right after a `planned_at` reschedule is
    applied. `baseline_nodes`/`baseline_edges` must reflect the graph as it
    was *before* that reschedule (the caller snapshots them pre-mutation —
    autoflush would otherwise make a fresh query see the already-changed
    date, defeating the whole "what did this used to look like" comparison).
    Returns None (no Risk raised) if nothing beyond the edited milestone
    itself is actually disturbed — a routine reschedule with slack to spare
    is not a finding, same "not enough signal" posture forecast.py's own
    heuristic takes. No `actor_id`: like every other detector in this
    package, this is a system finding, not a human action — the PM who
    edited the date is already audited separately by
    app/twin/audit.py's record_twin_audit_event (milestone_override)."""
    result = propagate(baseline_nodes, baseline_edges, milestone.id, new_date)
    downstream = [a for a in result.affected if a.milestone_id != milestone.id]
    if not downstream:
        return None

    names = dict(
        (
            await session.execute(
                select(Milestone.id, Milestone.name).where(Milestone.project_id == project.id)
            )
        ).all()
    )

    baseline_twin = compute_twin(baseline_nodes, baseline_edges)
    baseline_slack = {node_id: n.slack_days for node_id, n in baseline_twin.nodes.items()}
    severity = "critical" if any(_now_critical(a, baseline_slack) for a in downstream) else "high"

    consequence = _format_consequence(milestone, names, result, downstream)

    risk, created = await create_or_supersede_risk(
        session,
        project_id=project.id,
        source="propagation",
        finding_key=f"propagation:{milestone.id}",
        severity=severity,
        downstream_consequence=consequence,
        milestone_id=milestone.id,
        detail={
            "shifted_milestone_id": str(milestone.id),
            "new_date": new_date.isoformat(),
            "binding_constraint_after": (
                str(result.binding_constraint_after) if result.binding_constraint_after else None
            ),
            "affected": [
                {
                    "milestone_id": str(a.milestone_id),
                    "milestone_name": names.get(a.milestone_id, str(a.milestone_id)),
                    "previous_earliest": a.previous_earliest.isoformat() if a.previous_earliest else None,
                    "new_earliest": a.new_earliest.isoformat() if a.new_earliest else None,
                    "consumed_slack_days": a.consumed_slack_days,
                    "propagation_stopped": a.propagation_stopped,
                }
                for a in downstream
            ],
        },
    )

    if created:
        # Dispatch the risk event *before* draft_deviation (which raises its
        # own "deviation" event internally): create_notification's FR-NTF-03
        # collapsing only upgrades an existing pending notification's
        # severity/consequence when the new event is *more* severe
        # (app/foresight/notification.py's own comment) — dispatch_event
        # hardcodes a flat "medium" for a bare deviation event, so doing this
        # the other way round would silently downgrade a critical/high
        # propagation finding down to "medium" the moment the deviation's
        # own notification collapsed into it.
        await dispatch_event(session, project=project, event_type="risk", risk=risk)
        try:
            await draft_deviation(
                session,
                project=project,
                class_code="delay",
                description_en=consequence,
                risk=risk,
                milestone_id=milestone.id,
                evidence_text=(
                    f"Automatic downstream-impact detection: {milestone.name!r} rescheduled to "
                    f"{new_date.isoformat()}, computed via app/twin/graph.py's propagate()."
                ),
            )
        except UnknownDeviationClass:
            # "delay" is currently only seeded for the event-production
            # vertical (seed_data/deviation_classes.py's own docstring — a
            # second vertical is a named future addition, not yet real).
            # The Twin/Risk/Notification path above is fully vertical-
            # agnostic and must not fail because a *different* vertical
            # hasn't seeded its own deviation_class terms yet — the Risk
            # already exists and already carries the full downstream
            # consequence; only the optional structured Deviation link is
            # skipped, same "degrade honestly, don't crash the request"
            # posture the rest of this codebase takes elsewhere (e.g.
            # Risk.base_rate staying None with no fabricated value).
            pass

    return risk
