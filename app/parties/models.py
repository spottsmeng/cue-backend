import uuid
from datetime import datetime

from sqlalchemy import Enum, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID

from app.core.base import Base, TZDateTime, UUIDPk

# CUE-PRD.md §6.13 (FR-VRG). Own module, not app/models/party.py — same
# per-domain-module-owns-its-models precedent app/twin/, app/documents/,
# app/foresight/ and app/reports/ already established; Party/ChannelIdentity
# predate this milestone and stay where they are, but VendorMetric is new
# here.
VendorMetricName = Enum(
    "median_response_time_days",
    "on_time_rate",
    "revision_churn",
    "price_drift_pct",
    "deviation_frequency",
    name="vendor_metric_name",
)


class VendorMetric(Base, UUIDPk):
    """FR-VRG-01/02/03: one immutable snapshot of a single computed metric,
    for one vendor, as of `computed_at`.

    Append-only, not upsert-in-place — a fresh row is written every time
    `app/parties/service.py`'s `recompute_vendor_metrics` runs (FR-VRG-03:
    "update continuously as commitments resolve"), which is what makes
    §11.2's `/parties/{id}/reliability` "history" operation possible at all;
    "metrics" (the current snapshot) is just the latest row per (metric,
    segment_event_archetype) pair — see app/parties/reliability.py's
    `DISTINCT ON` query. No update/delete path exists anywhere in this
    module, same append-only-by-omission posture app/reports/models.py's
    ReportSnapshot documents (that one adds a DB trigger to make it a hard
    guarantee; this table doesn't need one — nothing else holds a FK into
    it, and an accidental mutation here has no downstream integrity
    consequence the way a report snapshot's would).

    `value IS NULL` with `unavailable_reason` set is this table's
    concrete instance of CLAUDE.md's "don't fabricate what you don't have"
    rule — a metric genuinely uncomputable right now (too few data points,
    or, for revision_churn/price_drift specifically, `Commitment.supersedes`
    structurally unpopulated because FR-LED-05 doesn't exist yet — see
    app/parties/compute.py's module docstring) is a real row naming why,
    never a fabricated 0.0 a caller could mistake for "no churn happened."

    Scoped directly by `organisation_id` (denormalised from `Party` at
    compute time), not a project join — Party itself is org-scoped, not
    project-scoped ("the same vendor contact is one row across every
    project they appear in", app/models/party.py's own docstring), and a
    vendor's reliability score is deliberately computed across every
    project they've touched in this org, not narrowed to one. Follow the
    `organisations`/`foresight_thresholds` direct-column RLS pattern, not
    the `commitments` project-join one.

    `segment_vendor_category` / `segment_city` are denormalised from
    `Party` at compute time too (not looked up live) — both are constants
    for a given party, so every row for that party carries the same value,
    which is what lets a caller filter this table by them without a join,
    and is also historically honest (what the party's category/city *was*
    when this metric was computed, even if a caller edits Party later).
    `segment_event_archetype` is the one axis that genuinely varies row to
    row: one row per metric has it NULL ("all archetypes combined"), plus
    one additional row per distinct `Project.archetype_code` found among
    this vendor's own commitments as of this recompute.
    """

    __tablename__ = "vendor_metrics"

    organisation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organisations.id"), index=True, comment="RLS scope"
    )
    party_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("parties.id"), index=True
    )
    metric: Mapped[str] = mapped_column(VendorMetricName, index=True)
    value: Mapped[float | None] = mapped_column(default=None)
    sample_size: Mapped[int] = mapped_column(default=0)
    unavailable_reason: Mapped[str | None] = mapped_column(default=None)

    segment_vendor_category: Mapped[str | None] = mapped_column(default=None)
    segment_city: Mapped[str | None] = mapped_column(default=None)
    segment_event_archetype: Mapped[str | None] = mapped_column(default=None)

    computed_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
