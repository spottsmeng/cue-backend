"""The read path onto `vendor_metrics` — §11.2's `/parties/{id}/reliability`
(app/api/parties.py) and app/reports/composer.py's vendor status section
both call `get_reliability_metrics` directly, against the already RLS-scoped
session either caller passes in (never a second connection), same "no
parallel query path" discipline app/reports/composer.py's own module
docstring establishes for every other section of that report.

Reads only — `app/parties/service.py`'s `recompute_vendor_metrics` is the
only writer. Deliberately does not recompute on read: a metric snapshot is
only as fresh as the last commitment transition that triggered it
(FR-VRG-03), and re-deriving it here on every GET would let this module's
answer and the write path's silently drift from each other the same class
of bug the report composer's own docstring warns against for a parallel
query path.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import OntologyTerm, Party
from app.parties.models import VendorMetric


@dataclass(frozen=True)
class MetricSnapshot:
    metric: str
    value: float | None
    available: bool
    unavailable_reason: str | None
    sample_size: int
    computed_at: datetime | None
    segment_vendor_category: str | None
    segment_city: str | None
    segment_event_archetype: str | None


def _snapshot(row: VendorMetric) -> MetricSnapshot:
    return MetricSnapshot(
        metric=row.metric,
        value=row.value,
        available=row.value is not None,
        unavailable_reason=row.unavailable_reason,
        sample_size=row.sample_size,
        computed_at=row.computed_at,
        segment_vendor_category=row.segment_vendor_category,
        segment_city=row.segment_city,
        segment_event_archetype=row.segment_event_archetype,
    )


async def _segment_matches_party(
    session: AsyncSession, party_id: uuid.UUID, *, vendor_category: str | None, city: str | None
) -> bool:
    """`vendor_category`/`city` are constants per Party (see VendorMetric's
    own docstring), so filtering a single vendor's own endpoint by a value
    that doesn't match that vendor is a legitimate "no data for this
    segment" rather than a distinct per-row filter — checked once against
    Party directly rather than per VendorMetric row."""
    if vendor_category is None and city is None:
        return True
    party = (await session.execute(select(Party).where(Party.id == party_id))).scalar_one_or_none()
    if party is None:
        return False
    if city is not None and party.city != city:
        return False
    if vendor_category is not None:
        code = (
            await session.execute(
                select(OntologyTerm.code).where(OntologyTerm.id == party.vendor_category_term_id)
            )
        ).scalar_one_or_none()
        if code != vendor_category:
            return False
    return True


async def get_reliability_metrics(
    session: AsyncSession,
    party_id: uuid.UUID,
    *,
    event_archetype: str | None = None,
    vendor_category: str | None = None,
    city: str | None = None,
) -> dict[str, MetricSnapshot]:
    """FR-VRG-02/05: the current snapshot — the latest VendorMetric row per
    metric name, for one segment (`event_archetype=None` is the "every
    archetype combined" row every recompute always writes). Empty dict for
    a `vendor_category`/`city` filter that doesn't match this vendor's own
    values (see `_segment_matches_party`), or for a party with no metrics
    computed yet at all (never recomputed, or a non-vendor Party)."""
    if not await _segment_matches_party(
        session, party_id, vendor_category=vendor_category, city=city
    ):
        return {}

    stmt = (
        select(VendorMetric)
        .where(VendorMetric.party_id == party_id, VendorMetric.segment_event_archetype == event_archetype)
        .distinct(VendorMetric.metric)
        .order_by(VendorMetric.metric, VendorMetric.computed_at.desc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    return {row.metric: _snapshot(row) for row in rows}


async def get_reliability_history(
    session: AsyncSession,
    party_id: uuid.UUID,
    *,
    metric: str | None = None,
    event_archetype: str | None = None,
) -> list[MetricSnapshot]:
    """§11.2's "history" operation: every VendorMetric row ever written for
    this vendor in this segment, oldest first — the trend a Procurement
    reviewer actually wants ("has this vendor gotten slower to respond over
    the last six months"), which the append-only write path
    (app/parties/service.py) makes possible for free."""
    stmt = select(VendorMetric).where(
        VendorMetric.party_id == party_id, VendorMetric.segment_event_archetype == event_archetype
    )
    if metric is not None:
        stmt = stmt.where(VendorMetric.metric == metric)
    stmt = stmt.order_by(VendorMetric.computed_at.asc())
    rows = (await session.execute(stmt)).scalars().all()
    return [_snapshot(row) for row in rows]
