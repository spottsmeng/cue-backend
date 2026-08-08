import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

# Mirrors app/parties/models.py's VendorMetricName native enum, same
# separation every other *Literal in app/api/schemas.py already draws from
# its SQLAlchemy Enum counterpart.
VendorMetricNameLiteral = Literal[
    "median_response_time_days", "on_time_rate", "revision_churn",
    "price_drift_pct", "deviation_frequency",
]


class VendorMetricOut(BaseModel):
    """One metric's current or historical snapshot. `value=None` with
    `available=False` and `unavailable_reason` set (never a fabricated
    0.0) is this endpoint's concrete instance of CLAUDE.md's "don't
    fabricate what you don't have" rule — see app/parties/compute.py's
    module docstring for the two concrete cases (too small a sample,
    or FR-LED-05's supersedes chain not existing yet)."""

    metric: VendorMetricNameLiteral
    value: float | None
    available: bool
    unavailable_reason: str | None
    sample_size: int
    computed_at: datetime | None
    segment_vendor_category: str | None
    segment_city: str | None
    segment_event_archetype: str | None


class VendorReliabilityOut(BaseModel):
    """§11.2's `/parties/{id}/reliability` "metrics" operation: the current
    snapshot of every metric CUE has ever been able to compute for this
    vendor, for one segment. A metric this vendor has literally never had
    computed at all (recompute never ran, e.g. no commitment of theirs has
    ever transitioned) is simply absent from `metrics` — distinct from a
    metric that *was* computed but came back structurally unavailable
    (present, `available=False`)."""

    party_id: uuid.UUID
    event_archetype: str | None
    vendor_category: str | None
    city: str | None
    metrics: list[VendorMetricOut]


class VendorReliabilityHistoryOut(BaseModel):
    """§11.2's "history" operation — every snapshot ever written for this
    vendor in this segment, oldest first (app/parties/reliability.py's
    `get_reliability_history`)."""

    party_id: uuid.UUID
    event_archetype: str | None
    metric: VendorMetricNameLiteral | None
    history: list[VendorMetricOut]
