"""Bridges a real captured Message into app/ledger/extractor.py's existing
extract_case — CLAUDE.md: "real capture doesn't change the extraction
contract at all (same schema, same prompt), it only changes what feeds it."
This module is exactly that feed: it builds the same ProjectContext/
FixtureCase TypedDict shapes cue-eval's fixtures already produce
(app/capture/fixtures.py), from a real Project/Message/Party, so extract_case
itself needs no real-capture-aware branch and stays completely untouched —
CLAUDE.md and Prompt 11b both name app/ledger/extractor.py's core contract as
off-limits to this session.

extract_case's own `case["party"]` resolves a Commitment's vendor Party via
`_get_or_create_party`'s exact-string display-name match — a real-capture
message's party is instead resolved up front by app/capture/identity.py's
resolve_identity (FR-NRM-03), which is a strict superset of that exact-match
(same lookup key: organisation_id + display_name), so build_case passes the
already-resolved Party's own `display_name` verbatim. The two converge on
the *same* Party row without extract_case needing to know a resolver ran at
all — `_get_or_create_party`'s lookup finds the row resolve_identity already
created rather than minting a second one.
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.capture.fixtures import FixtureCase, ProjectContext
from app.capture.models import Message
from app.models import Milestone, Project


async def build_project_context(session: AsyncSession, project: Project) -> ProjectContext:
    """The real-project counterpart to cue-eval's fixture project_context —
    same shape, built from live Project/Milestone rows instead of
    cases.json. `build_up` has no real-project analogue yet (no column on
    Project names a distinct bump-in date range the way the fixture's
    project_context.json does) — left empty rather than guessed at,
    matching CUE-PRD.md's own "don't fabricate a distribution/date you
    don't have" posture (FR-TWN-08's build note, CLAUDE.md's Models table)
    applied here to a prompt-context field instead of a metric.
    """
    milestones = (
        await session.execute(
            select(Milestone)
            .where(Milestone.project_id == project.id)
            .order_by(Milestone.planned_at.asc().nulls_last())
        )
    ).scalars().all()

    return ProjectContext(
        project=project.name,
        client=project.client_name or "",
        timezone=project.timezone,
        venue=project.venue or "",
        build_up=[],
        event_days=(
            [project.event_start.date().isoformat()]
            if project.event_start is not None
            else []
        ),
        doors=project.event_start.isoformat() if project.event_start is not None else "",
        known_milestones=[
            {"name": m.name, "due": m.planned_at.isoformat() if m.planned_at else ""}
            for m in milestones
        ],
        vendors=[],
    )


def build_case(message: Message, *, channel_type: str, party_display_name: str) -> FixtureCase:
    """The real-message counterpart to a cue-eval fixture case. `band` is
    "live" — extract_case itself never reads `case["band"]` (only
    scripts/extract_fixtures.py's own print statements do, for cue-eval's
    labelling), so this is purely an honest marker for anyone inspecting a
    real-capture case dict, not a value the extraction path branches on.
    `sent_weekday` is derived from `message.sent_at` (a real timestamp,
    unlike a fixture case which has to state it explicitly) — a same-day
    relative-date resolution ("delivery by Friday") benefits from the same
    hint build_prompt already gives fixture cases.
    """
    return FixtureCase(
        id=str(message.id),
        band="live",
        lang=message.language or "und",
        channel=channel_type,
        party=party_display_name,
        sent_at=message.sent_at.isoformat(),
        message=message.text or "",
        sent_weekday=message.sent_at.strftime("%A"),
    )
