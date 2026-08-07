import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.foresight.models import ForesightThreshold
from app.models import Project

# FR-FOR-07: "Support configurable thresholds per project and per deviation
# class." Conservative, documented fallbacks — used only when no
# ForesightThreshold row exists for a given (project, deviation class,
# metric) combination, i.e. before a PM has ever configured one. These are
# not fabricated precision; they're a defensible starting point (same
# posture app/twin/graph.py's own module docstring takes for its heuristics)
# that a PM can override per project/class via
# POST /admin/foresight-thresholds.
DEFAULT_THRESHOLDS: dict[str, float] = {
    # Silence Radar (FR-FOR-01): a gap this many multiples of a vendor's own
    # historical response baseline, with no response, is "expected but
    # absent."
    "silence_multiplier": 2.0,
    # Escalation (FR-FOR-08): hours an open Risk can go unacknowledged
    # before escalating to the next role up.
    "escalation_hours": 24.0,
    # Forecasting (FR-FOR-06): a milestone at or below this many days of
    # slack is treated as at high miss-likelihood when combined with a
    # Silence Radar flag — see app/foresight/forecast.py's documented
    # heuristic.
    "forecast_slack_days": 3.0,
}


async def resolve_threshold(
    session: AsyncSession,
    *,
    project: Project,
    metric: str,
    deviation_class_term_id: uuid.UUID | None = None,
) -> float:
    """Most-specific-match resolution over the project x deviation_class
    axes — mirrors app/documents/service.py's _resolve_retention_policy
    (org x vertical there) and app/twin/service.py's _most_specific ranking,
    adapted to this table's two-axis shape. A row naming this exact project
    beats one with project_id NULL (org-wide default); a row naming this
    exact deviation_class beats one with deviation_class_term_id NULL
    (applies to every class). Falls back to DEFAULT_THRESHOLDS when no row
    at all matches — see that dict's own docstring for why that's an
    honest, documented default rather than a fabricated one.
    """
    stmt = select(ForesightThreshold).where(
        ForesightThreshold.organisation_id == project.organisation_id,
        ForesightThreshold.metric == metric,
        or_(ForesightThreshold.project_id == project.id, ForesightThreshold.project_id.is_(None)),
        or_(
            ForesightThreshold.deviation_class_term_id == deviation_class_term_id,
            ForesightThreshold.deviation_class_term_id.is_(None),
        ),
    )
    candidates = (await session.execute(stmt)).scalars().all()
    if not candidates:
        return DEFAULT_THRESHOLDS[metric]

    def _specificity(t: ForesightThreshold) -> int:
        return (1 if t.project_id is not None else 0) + (
            1 if t.deviation_class_term_id is not None else 0
        )

    return max(candidates, key=_specificity).value
