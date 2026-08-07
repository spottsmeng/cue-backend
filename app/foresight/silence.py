from datetime import datetime, timezone as dt_timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.foresight.consequence import commitment_downstream_consequence
from app.foresight.notification import dispatch_event
from app.foresight.risk import create_or_supersede_risk
from app.foresight.threshold import resolve_threshold
from app.ledger import lifecycle
from app.models import Commitment, Evidence, Project
from app.foresight.models import Risk

# FR-FOR-01/02 (PRD §6.9): Silence Radar. Detects an expected-but-absent
# response by comparing the live gap since a vendor's last contact against
# that vendor's *own* historical response-time baseline — not a fixed
# calendar rule, since different vendors genuinely communicate at different
# cadences and a single global threshold would either miss slow-cadence
# vendors' real silences or false-positive on fast-cadence ones going quiet
# for an ordinary weekend.
#
# Scoped to `committed` commitments only — FR-LCY-02 is specifically the
# committed -> at_risk transition, and "expected response" only means
# something once there's a live obligation to respond about (a `proposed`
# offer going quiet is a sales problem, not yet a delivery risk).
#
# The baseline sample is this project's own history with the vendor, not
# the vendor's history across every project in the org — simpler to reason
# about and test; widening the sample once there's demand for it (a vendor
# genuinely fresh to this project but well-known elsewhere in the org) is a
# documented, deliberate follow-on, not an oversight.


async def compute_vendor_baseline(session: AsyncSession, project_id, party_id) -> float | None:
    """Median gap (days) between consecutive Evidence.sent_at timestamps
    across this vendor's commitments in this project. None ("no baseline
    yet") when fewer than 3 timestamps exist — two points give exactly one
    delta, not enough to call a pattern a baseline."""
    stmt = (
        select(Evidence.sent_at)
        .join(Commitment, Commitment.id == Evidence.commitment_id)
        .where(Commitment.project_id == project_id, Commitment.party_id == party_id)
        .order_by(Evidence.sent_at)
    )
    timestamps = (await session.execute(stmt)).scalars().all()
    if len(timestamps) < 3:
        return None
    deltas = sorted(
        (b - a).total_seconds() / 86400.0 for a, b in zip(timestamps, timestamps[1:]) if b > a
    )
    if not deltas:
        return None
    mid = len(deltas) // 2
    return deltas[mid] if len(deltas) % 2 else (deltas[mid - 1] + deltas[mid]) / 2


async def compute_base_rate(session: AsyncSession, project_id, party_id) -> float | None:
    """FR-FOR-02: fraction of this vendor's past silence-flagged
    commitments (in this project) that went on to `broken` rather than
    recovering (`delivered`/`renegotiated`). None below a minimum sample of
    3 — CLAUDE.md's Models table and app/twin/graph.py's own precedent both
    say: degrade honestly rather than report a rate computed from noise."""
    stmt = (
        select(Commitment.state)
        .join(Risk, Risk.commitment_id == Commitment.id)
        .where(
            Risk.source == "silence",
            Commitment.project_id == project_id,
            Commitment.party_id == party_id,
            Commitment.state.in_(["broken", "delivered", "renegotiated"]),
        )
    )
    outcomes = (await session.execute(stmt)).scalars().all()
    if len(outcomes) < 3:
        return None
    broken = sum(1 for s in outcomes if s == "broken")
    return broken / len(outcomes)


def _severity_for_ratio(ratio: float) -> str:
    if ratio >= 3.0:
        return "critical"
    if ratio >= 2.0:
        return "high"
    return "medium"


async def scan_silence(session: AsyncSession, project: Project) -> list[Risk]:
    """Runs Silence Radar for one project — called both by the arq-scheduled
    sweep (app/foresight/worker.py) and directly by tests. Returns every
    Risk touched (created or refreshed) this scan. Does not commit — caller
    controls the transaction boundary, same convention as every other
    service function in this codebase."""
    now = datetime.now(dt_timezone.utc)
    commitments = (
        await session.execute(select(Commitment).where(Commitment.project_id == project.id, Commitment.state == "committed"))
    ).scalars().all()

    touched: list[Risk] = []
    baseline_cache: dict = {}
    for commitment in commitments:
        if commitment.party_id not in baseline_cache:
            baseline_cache[commitment.party_id] = await compute_vendor_baseline(
                session, project.id, commitment.party_id
            )
        baseline_days = baseline_cache[commitment.party_id]
        if baseline_days is None:
            continue  # FR-FOR-02: no base rate/baseline yet — nothing to compare against

        last_sent_at = (
            await session.execute(
                select(Evidence.sent_at)
                .where(Evidence.commitment_id == commitment.id)
                .order_by(Evidence.sent_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if last_sent_at is None:
            continue

        multiplier = await resolve_threshold(session, project=project, metric="silence_multiplier")
        threshold_days = baseline_days * multiplier
        gap_days = (now - last_sent_at).total_seconds() / 86400.0
        if gap_days <= threshold_days:
            continue

        base_rate = await compute_base_rate(session, project.id, commitment.party_id)
        consequence = await commitment_downstream_consequence(
            session, project, commitment,
            because=(
                f"No response from this vendor in {gap_days:.1f} days "
                f"(baseline {baseline_days:.1f}d x{multiplier:g})"
            ),
        )
        risk, created = await create_or_supersede_risk(
            session,
            project_id=project.id,
            source="silence",
            finding_key=f"silence:{commitment.id}",
            severity=_severity_for_ratio(gap_days / threshold_days),
            downstream_consequence=consequence,
            base_rate=base_rate,
            commitment_id=commitment.id,
            detail={
                "gap_days": round(gap_days, 2),
                "baseline_days": round(baseline_days, 2),
                "multiplier": multiplier,
                "last_sent_at": last_sent_at.isoformat(),
            },
        )
        touched.append(risk)

        if created:
            await dispatch_event(session, project=project, event_type="risk", risk=risk)
            if commitment.state == "committed":
                # FR-LCY-02: silence past a vendor's own baseline is one of
                # the three named automatic triggers for committed -> at_risk.
                await lifecycle.apply_automatic_transition(
                    session,
                    project_id=project.id,
                    commitment=commitment,
                    to_state="at_risk",
                    trigger="silence",
                    detail={"risk_id": str(risk.id), "gap_days": round(gap_days, 2)},
                )

    return touched
