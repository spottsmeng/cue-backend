"""CUE-PRD.md §6.13 (FR-VRG-01): the five per-vendor metric computations,
each reading directly from tables other domains already own (Evidence/
Commitment/AuditLog from the Ledger, Deviation from Foresight) rather than a
second derived copy of any of them — this module is the single place that
turns that raw history into a number, called both by
app/parties/service.py's write/cache path (FR-VRG-03) and directly by
app/foresight/silence.py (FR-VRG-04) for a fresh, uncached read.

Every function returns a `MetricResult`, never a bare float — `value=None`
with `unavailable_reason` set is how this module says "cannot honestly
compute this yet," the same "don't fabricate what you don't have" posture
CLAUDE.md's Models table and app/foresight/silence.py's own base_rate
already establish elsewhere in this codebase. In particular:

  - `revision_churn` and `price_drift_pct` both depend on
    `Commitment.supersedes` (PRD FR-LED-05: "detect and link supersession —
    a later commitment replacing an earlier one"). As of this session that
    column has never been written to by any extraction or API path in this
    codebase (confirmed by grep before writing this module) — FR-LED-05
    itself is not implemented. Per Prompt 10's own explicit instruction,
    this session takes the "structurally absent, not zero" option rather
    than building a supersession-linking mechanism as a side effect of the
    Vendor Reliability Graph: `_supersedes_data_exists` below is a real,
    live structural check (does *any* commitment anywhere in this
    organisation have a non-empty `supersedes`?), not a hardcoded flag — the
    moment a future session populates that column, these two metrics start
    computing real numbers with no change needed here, the same
    `_VRG_AVAILABLE`-style "wired for the day the dependency arrives" idiom
    app/reports/composer.py already uses for this very milestone.
  - `deviation_frequency` degrades the same way if Foresight's own
    `Deviation` table isn't part of this build (an `ImportError` guard,
    exactly app/reports/composer.py's own pattern for the same reason).
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog, Commitment, Evidence, Project

try:
    from app.foresight.models import Deviation

    _DEVIATION_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only in a build without Foresight
    _DEVIATION_AVAILABLE = False


@dataclass(frozen=True)
class MetricResult:
    value: float | None
    sample_size: int
    unavailable_reason: str | None = None


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _scope_by_project_or_archetype(
    stmt, *, project_id: uuid.UUID | None, archetype_code: str | None
):
    """Shared filter every metric below applies: either narrowed to one
    project (app/foresight/silence.py's per-project fallback use), or to
    every commitment across the org whose project resolved to a given
    archetype (FR-VRG-02's segmentation), or neither (the org-wide, all-
    archetypes "overall" row). Never both at once — callers pick one axis."""
    if project_id is not None:
        return stmt.where(Commitment.project_id == project_id)
    if archetype_code is not None:
        return stmt.join(Project, Project.id == Commitment.project_id).where(
            Project.archetype_code == archetype_code
        )
    return stmt


async def compute_median_response_time_days(
    session: AsyncSession,
    party_id: uuid.UUID,
    *,
    project_id: uuid.UUID | None = None,
    archetype_code: str | None = None,
) -> MetricResult:
    """FR-VRG-01's median response time. Median gap (days) between
    consecutive Evidence.sent_at timestamps across this vendor's
    commitments — the exact computation app/foresight/silence.py's
    `compute_vendor_baseline` used to own outright; that function now
    delegates here (project-scoped) rather than duplicating the query, per
    Prompt 10's own "reuse this computation rather than writing a second
    one" instruction. None below 3 timestamps — two points give exactly one
    delta, not enough to call a pattern a baseline (same threshold
    `compute_vendor_baseline` already used)."""
    stmt = (
        select(Evidence.sent_at)
        .join(Commitment, Commitment.id == Evidence.commitment_id)
        .where(Commitment.party_id == party_id)
    )
    stmt = _scope_by_project_or_archetype(
        stmt, project_id=project_id, archetype_code=archetype_code
    )
    timestamps = (await session.execute(stmt.order_by(Evidence.sent_at))).scalars().all()
    if len(timestamps) < 3:
        return MetricResult(None, len(timestamps), "fewer than 3 evidence timestamps for this vendor")

    deltas = sorted(
        (b - a).total_seconds() / 86400.0 for a, b in zip(timestamps, timestamps[1:]) if b > a
    )
    if not deltas:
        return MetricResult(None, len(timestamps), "no positive time gaps between timestamps")
    return MetricResult(_median(deltas), len(timestamps))


async def compute_on_time_rate(
    session: AsyncSession,
    party_id: uuid.UUID,
    *,
    project_id: uuid.UUID | None = None,
    archetype_code: str | None = None,
) -> MetricResult:
    """FR-VRG-01's on-time commitment rate, from lifecycle transition
    history. Denominator: this vendor's commitments that reached a
    delivery-relevant outcome (`delivered` or `broken`) *and* had a due
    date to be judged against — a commitment with no `due_at` was never
    given a deadline to keep, so it cannot be scored either way. Numerator:
    the `delivered` ones whose AuditLog `state_transition` row into
    `delivered` landed at or before `due_at` (at most one such row can ever
    exist per commitment — `delivered` is a terminal state,
    app/ledger/lifecycle.py's TRANSITIONS). A currently-`broken` commitment
    always counts against the rate, even one that may later recover via
    `renegotiated` — this metric recomputes on every transition (FR-VRG-03),
    so it self-corrects the moment that happens; there is no reason to
    special-case a state that might change later differently from one that
    already has.
    """
    stmt = select(Commitment).where(
        Commitment.party_id == party_id,
        Commitment.state.in_(("delivered", "broken")),
        Commitment.due_at.is_not(None),
    )
    stmt = _scope_by_project_or_archetype(
        stmt, project_id=project_id, archetype_code=archetype_code
    )
    commitments = (await session.execute(stmt)).scalars().all()
    if not commitments:
        return MetricResult(None, 0, "no delivered/broken commitments with a due date yet")

    delivered_ids = [c.id for c in commitments if c.state == "delivered"]
    delivered_at_by_id: dict[uuid.UUID, object] = {}
    if delivered_ids:
        rows = (
            await session.execute(
                select(AuditLog.commitment_id, AuditLog.occurred_at).where(
                    AuditLog.commitment_id.in_(delivered_ids),
                    AuditLog.action == "state_transition",
                    AuditLog.to_state == "delivered",
                )
            )
        ).all()
        for commitment_id, occurred_at in rows:
            delivered_at_by_id[commitment_id] = occurred_at

    on_time = 0
    for c in commitments:
        if c.state != "delivered":
            continue  # broken always counts against the rate
        delivered_at = delivered_at_by_id.get(c.id)
        if delivered_at is not None and delivered_at <= c.due_at:
            on_time += 1

    return MetricResult(on_time / len(commitments), len(commitments))


async def _supersedes_data_exists(session: AsyncSession) -> bool:
    """See this module's own docstring: a live check, not a hardcoded flag —
    RLS-scoped, so this only sees the caller's own organisation, same as
    every other query in this module."""
    row = (
        await session.execute(
            select(Commitment.id).where(func.cardinality(Commitment.supersedes) > 0).limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


_SUPERSEDES_UNAVAILABLE_REASON = (
    "revision/supersession chains are not yet computable: Commitment.supersedes is not "
    "populated by any extraction or API path in this build (PRD FR-LED-05 is not "
    "implemented — see backend/PROGRESS.md's M7 notes). Structurally absent, not zero."
)


async def compute_revision_churn(
    session: AsyncSession,
    party_id: uuid.UUID,
    *,
    project_id: uuid.UUID | None = None,
    archetype_code: str | None = None,
) -> MetricResult:
    """FR-VRG-01's revision churn: the fraction of this vendor's commitments
    that are themselves a later revision of an earlier one (a non-empty
    `Commitment.supersedes`). See this module's own docstring for why this
    degrades to `unavailable` rather than `0.0` while FR-LED-05 remains
    unimplemented."""
    if not await _supersedes_data_exists(session):
        return MetricResult(None, 0, _SUPERSEDES_UNAVAILABLE_REASON)

    stmt = select(Commitment).where(Commitment.party_id == party_id)
    stmt = _scope_by_project_or_archetype(
        stmt, project_id=project_id, archetype_code=archetype_code
    )
    commitments = (await session.execute(stmt)).scalars().all()
    if not commitments:
        return MetricResult(None, 0, "no commitments for this vendor yet")

    revised = sum(1 for c in commitments if len(c.supersedes) > 0)
    return MetricResult(revised / len(commitments), len(commitments))


async def compute_price_drift(
    session: AsyncSession,
    party_id: uuid.UUID,
    *,
    project_id: uuid.UUID | None = None,
    archetype_code: str | None = None,
) -> MetricResult:
    """FR-VRG-01's price drift: mean absolute percentage change in `amount`
    across a commitment's renegotiation chain, walked via `supersedes` —
    same FR-LED-05 dependency and same degrade-honestly posture as
    `compute_revision_churn` above, and for the same reason (a chain is
    only knowable once supersession links exist)."""
    if not await _supersedes_data_exists(session):
        return MetricResult(None, 0, _SUPERSEDES_UNAVAILABLE_REASON)

    stmt = select(Commitment).where(
        Commitment.party_id == party_id, func.cardinality(Commitment.supersedes) > 0
    )
    stmt = _scope_by_project_or_archetype(
        stmt, project_id=project_id, archetype_code=archetype_code
    )
    revising_commitments = (await session.execute(stmt)).scalars().all()
    if not revising_commitments:
        return MetricResult(None, 0, "no revision chains for this vendor yet")

    superseded_ids = {sid for c in revising_commitments for sid in c.supersedes}
    superseded_by_id: dict[uuid.UUID, Commitment] = {}
    if superseded_ids:
        rows = (
            await session.execute(select(Commitment).where(Commitment.id.in_(superseded_ids)))
        ).scalars().all()
        superseded_by_id = {c.id: c for c in rows}

    drifts: list[float] = []
    for c in revising_commitments:
        if c.amount is None:
            continue
        for superseded_id in c.supersedes:
            prior = superseded_by_id.get(superseded_id)
            if prior is None or prior.amount is None or prior.amount == 0:
                continue
            drifts.append(abs(float(c.amount) - float(prior.amount)) / float(prior.amount))

    if not drifts:
        return MetricResult(None, 0, "no revision chain had comparable amounts on both ends")
    return MetricResult(sum(drifts) / len(drifts) * 100.0, len(drifts))


async def compute_deviation_frequency(
    session: AsyncSession,
    party_id: uuid.UUID,
    *,
    project_id: uuid.UUID | None = None,
    archetype_code: str | None = None,
) -> MetricResult:
    """FR-VRG-01's deviation frequency: deviations attributable to this
    vendor (via `Deviation.commitment_id -> Commitment.party_id`; a
    deviation with no commitment link isn't attributable to any vendor and
    is excluded) as a fraction of this vendor's total commitments. Degrades
    to `unavailable` if Foresight's Deviation table isn't part of this
    build at all — same `ImportError`-guard pattern
    app/reports/composer.py's module-level `_VRG_AVAILABLE` check already
    establishes for this exact "does a later milestone exist yet" question.
    """
    if not _DEVIATION_AVAILABLE:
        return MetricResult(
            None, 0, "Foresight (M4, backend/PROGRESS.md) is not part of this build"
        )

    stmt = select(Commitment.id).where(Commitment.party_id == party_id)
    stmt = _scope_by_project_or_archetype(
        stmt, project_id=project_id, archetype_code=archetype_code
    )
    commitment_ids = (await session.execute(stmt)).scalars().all()
    if not commitment_ids:
        return MetricResult(None, 0, "no commitments for this vendor yet")

    deviation_count = (
        await session.execute(
            select(func.count()).select_from(Deviation).where(
                Deviation.commitment_id.in_(commitment_ids)
            )
        )
    ).scalar_one()
    return MetricResult(deviation_count / len(commitment_ids), len(commitment_ids))
