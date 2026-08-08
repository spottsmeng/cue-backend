"""FR-VRG-03: "update continuously as commitments resolve." `recompute_vendor_metrics`
is the single write path onto `vendor_metrics` — called from the same two call
sites app/twin/service.py's `recompute_on_commitment_transition` is (a manual
transition via app/api/commitments.py's `transition_commitment`, and an
automatic one via app/ledger/lifecycle.py's `apply_automatic_transition`), so
a metric refresh follows every commitment state change regardless of who or
what triggered it, not on a batch schedule.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Commitment, OntologyTerm, Party, Project
from app.parties.compute import (
    MetricResult,
    compute_deviation_frequency,
    compute_median_response_time_days,
    compute_on_time_rate,
    compute_price_drift,
    compute_revision_churn,
)
from app.parties.models import VendorMetric

_METRIC_FUNCS = {
    "median_response_time_days": compute_median_response_time_days,
    "on_time_rate": compute_on_time_rate,
    "revision_churn": compute_revision_churn,
    "price_drift_pct": compute_price_drift,
    "deviation_frequency": compute_deviation_frequency,
}


async def _distinct_archetype_codes(session: AsyncSession, party_id: uuid.UUID) -> list[str]:
    """FR-VRG-02's event-archetype segmentation axis: every distinct
    `Project.archetype_code` among this vendor's own commitments, across
    every project they've touched in this org — Party is org-scoped, so
    this is deliberately not narrowed to one project (see VendorMetric's
    own docstring)."""
    codes = (
        await session.execute(
            select(Project.archetype_code)
            .join(Commitment, Commitment.project_id == Project.id)
            .where(Commitment.party_id == party_id, Project.archetype_code.is_not(None))
            .distinct()
        )
    ).scalars().all()
    return sorted(codes)


async def _segment_tags(session: AsyncSession, party: Party) -> tuple[str | None, str | None]:
    """Denormalises Party's own category/city onto every VendorMetric row
    written this recompute — see that model's own docstring for why."""
    if party.vendor_category_term_id is None:
        return None, party.city
    term = (
        await session.execute(
            select(OntologyTerm.code).where(OntologyTerm.id == party.vendor_category_term_id)
        )
    ).scalar_one_or_none()
    return term, party.city


def _write_row(
    *,
    organisation_id: uuid.UUID,
    party_id: uuid.UUID,
    metric_name: str,
    result: MetricResult,
    vendor_category: str | None,
    city: str | None,
    archetype_code: str | None,
) -> VendorMetric:
    return VendorMetric(
        organisation_id=organisation_id,
        party_id=party_id,
        metric=metric_name,
        value=result.value,
        sample_size=result.sample_size,
        unavailable_reason=result.unavailable_reason,
        segment_vendor_category=vendor_category,
        segment_city=city,
        segment_event_archetype=archetype_code,
    )


async def recompute_vendor_metrics(session: AsyncSession, party_id: uuid.UUID) -> list[VendorMetric]:
    """Computes and inserts one fresh VendorMetric row per (metric,
    segment_event_archetype) pair — an "overall" row (archetype=None,
    every one of this vendor's commitments) plus one row per distinct
    archetype their commitments' projects have resolved to. Does not
    commit — same convention every other service function in this codebase
    follows; the caller (transition_commitment / apply_automatic_transition)
    owns the transaction boundary.

    Returns an empty list (a no-op, not an error) for a party_id that
    doesn't resolve to a real Party in the caller's own org — RLS already
    guarantees that lookup can't cross a tenant boundary."""
    party = (await session.execute(select(Party).where(Party.id == party_id))).scalar_one_or_none()
    if party is None:
        return []

    vendor_category, city = await _segment_tags(session, party)
    archetype_codes = await _distinct_archetype_codes(session, party_id)

    written: list[VendorMetric] = []
    for metric_name, compute_fn in _METRIC_FUNCS.items():
        for archetype_code in (None, *archetype_codes):
            result = await compute_fn(session, party_id, archetype_code=archetype_code)
            row = _write_row(
                organisation_id=party.organisation_id,
                party_id=party_id,
                metric_name=metric_name,
                result=result,
                vendor_category=vendor_category,
                city=city,
                archetype_code=archetype_code,
            )
            session.add(row)
            written.append(row)

    await session.flush()
    return written
