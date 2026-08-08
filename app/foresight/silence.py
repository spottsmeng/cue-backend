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
from app.parties.compute import compute_median_response_time_days

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
# FR-VRG-04: the baseline now prefers the Vendor Reliability Graph's own
# org-wide median response time (app/parties/compute.py's
# `compute_median_response_time_days`, the vendor's history across every
# project in this org) over this project's own narrower history — the
# "widening the sample... a documented, deliberate follow-on" this module's
# own comment used to promise here. Falls back to `compute_vendor_baseline`
# (this project's own history only) when the org-wide figure isn't
# computable yet (a vendor genuinely fresh everywhere in the org, or fewer
# than 3 evidence timestamps org-wide) — never silently treated as "no
# baseline at all" while a narrower-but-real one is available.


async def compute_vendor_baseline(session: AsyncSession, project_id, party_id) -> float | None:
    """Median gap (days) between consecutive Evidence.sent_at timestamps
    across this vendor's commitments *in this project only*. None ("no
    baseline yet") when fewer than 3 timestamps exist — two points give
    exactly one delta, not enough to call a pattern a baseline.

    FR-VRG-04: this used to own that computation outright; it now delegates
    to app/parties/compute.py's `compute_median_response_time_days`
    (project-scoped) rather than duplicating the query — same computation,
    same threshold, same result, per Prompt 10's own "reuse this
    computation rather than writing a second one" instruction. Kept as its
    own function (rather than inlined at both call sites) because
    tests/test_foresight_silence.py exercises it directly, and because
    `scan_silence` below still needs it by name as the fallback when the
    Vendor Reliability Graph's own org-wide baseline isn't computable yet.
    """
    result = await compute_median_response_time_days(session, party_id, project_id=project_id)
    return result.value


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
            # FR-VRG-04: prefer the Vendor Reliability Graph's own org-wide
            # baseline (this vendor's history across every project in the
            # org); fall back to this project's own narrower history only
            # when the org-wide figure isn't computable yet — see this
            # module's own top-of-file comment.
            org_wide = await compute_median_response_time_days(session, commitment.party_id)
            baseline_cache[commitment.party_id] = (
                org_wide.value
                if org_wide.value is not None
                else await compute_vendor_baseline(session, project.id, commitment.party_id)
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
