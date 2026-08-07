import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Commitment, Deliverable, Milestone, Project
from app.twin.graph import TwinResult
from app.twin.service import compute_current

# FR-FOR-09 / FR-NTF-02: "Never emit a bare alert... every surfaced item
# carries its computed downstream consequence." This module is the one place
# that turns a Twin slack/critical-path lookup into that consequence
# sentence, reused by every detector (silence.py, contradiction.py,
# forecast.py) rather than each inventing its own phrasing — consumes
# app/twin/service.py's compute_current, never recomputes CPM independently
# (Prompt 7's own instruction).


async def milestone_impact_text(
    twin: TwinResult, milestone: Milestone, *, because: str
) -> str:
    node = twin.nodes.get(milestone.id)
    if node is None or node.slack_days is None:
        return (
            f"{because} feeds {milestone.name!r}, but that milestone has no planned date yet, "
            "so its schedule impact cannot be computed."
        )
    if node.is_critical:
        return (
            f"{because} feeds {milestone.name!r}, which is on the critical path with zero slack — "
            "any further delay pushes that milestone's date directly."
        )
    return (
        f"{because} feeds {milestone.name!r}, which currently has {node.slack_days:.1f} days of "
        "slack — continued delay consumes that buffer and risks slipping the milestone."
    )


async def commitment_downstream_consequence(
    session: AsyncSession, project: Project, commitment: Commitment, *, because: str
) -> str:
    """Resolves the milestone a Commitment's Deliverable feeds (if any) and
    phrases its Twin impact via milestone_impact_text; falls back to a
    consequence naming the commitment's own deliverable when there is no
    milestone link to compute against — still never a bare event, just a
    narrower one."""
    if commitment.deliverable_id is not None:
        deliverable = (
            await session.execute(select(Deliverable).where(Deliverable.id == commitment.deliverable_id))
        ).scalar_one_or_none()
        if deliverable is not None and deliverable.milestone_id is not None:
            milestone = (
                await session.execute(select(Milestone).where(Milestone.id == deliverable.milestone_id))
            ).scalar_one_or_none()
            if milestone is not None:
                twin = await compute_current(session, project.id)
                return await milestone_impact_text(twin, milestone, because=because)
    return (
        f"{because} concerns {commitment.deliverable_en!r}, which is not linked to a Twin "
        "milestone, so no schedule impact beyond this commitment itself can be computed."
    )


async def milestone_id_downstream_consequence(
    session: AsyncSession, project: Project, milestone_id: uuid.UUID, *, because: str
) -> str:
    milestone = (await session.execute(select(Milestone).where(Milestone.id == milestone_id))).scalar_one_or_none()
    if milestone is None:
        return f"{because}, but the referenced milestone no longer exists."
    twin = await compute_current(session, project.id)
    return await milestone_impact_text(twin, milestone, because=because)


async def deliverable_downstream_consequence(
    session: AsyncSession, project: Project, deliverable_id: uuid.UUID | None, *, because: str
) -> str:
    """Same idea as commitment_downstream_consequence but for a finding
    anchored on a Deliverable directly (contradiction/spec-drift, which has
    no Commitment in play at all) rather than a Commitment."""
    if deliverable_id is not None:
        deliverable = (
            await session.execute(select(Deliverable).where(Deliverable.id == deliverable_id))
        ).scalar_one_or_none()
        if deliverable is not None and deliverable.milestone_id is not None:
            milestone = (
                await session.execute(select(Milestone).where(Milestone.id == deliverable.milestone_id))
            ).scalar_one_or_none()
            if milestone is not None:
                twin = await compute_current(session, project.id)
                return await milestone_impact_text(twin, milestone, because=because)
    return f"{because}, not linked to a Twin milestone, so no schedule impact beyond the spec itself can be computed."
