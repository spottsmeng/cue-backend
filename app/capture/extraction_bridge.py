"""Bridges a real captured Message into app/ledger/extractor.py's
extract_case — CLAUDE.md: "real capture doesn't change the extraction
contract at all (same schema, same prompt), it only changes what feeds it."
This module is exactly that feed: it builds the same ProjectContext/
FixtureCase TypedDict shapes cue-eval's fixtures already produce
(app/capture/fixtures.py), from a real Project/Message/Party, so extract_case
itself needs no real-capture-aware branch.

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
from app.models import Commitment, Milestone, Party, Project


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

    # The project's already-known vendors, by the same definition
    # app/ask/brief.py and app/reports/composer.py already use for "this
    # project's vendors" (a vendor_org Party with at least one commitment
    # here) — Party itself is organisation-scoped, so there is no direct
    # project->vendor column to read instead.
    #
    # This was `[]` until now, which quietly disabled
    # app/ledger/extractor.py's `_match_known_vendor`: on a real
    # team_collaboration message every model-named vendor failed to match
    # anything, so even a correctly-extracted, already-known vendor came back
    # unconfident and went to human review. Only cue-eval's fixture context
    # ever had this populated, so the eval measured a path production could
    # not reach.
    vendor_names = (
        await session.execute(
            select(Party.display_name)
            .join(Commitment, Commitment.party_id == Party.id)
            .where(Commitment.project_id == project.id, Party.type == "vendor_org")
            .distinct()
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
        vendors=[{"party": name} for name in vendor_names],
    )


def build_case(
    message: Message,
    *,
    channel_type: str,
    party_display_name: str,
    channel_capability: str | None = None,
) -> FixtureCase:
    """The real-message counterpart to a cue-eval fixture case. `band` is
    "live" — extract_case itself never reads `case["band"]` (only
    scripts/extract_fixtures.py's own print statements do, for cue-eval's
    labelling), so this is purely an honest marker for anyone inspecting a
    real-capture case dict, not a value the extraction path branches on.
    `sent_weekday` is derived from `message.sent_at` (a real timestamp,
    unlike a fixture case which has to state it explicitly) — a same-day
    relative-date resolution ("delivery by Friday") benefits from the same
    hint build_prompt already gives fixture cases.

    `channel_capability` (vendor-attribution-task.md): the caller's own
    lookup of this channel's `channel_types.capability` row, passed through
    untouched. `party_display_name` stays the message's *author* for every
    capability — including team_collaboration, where the author is internal
    staff, not the vendor — the branch that stops app/ledger/extractor.py
    from treating the author as the vendor lives entirely in
    `extract_case`/`_resolve_vendor_for_item`, keyed off this field; this
    function only carries the value, it doesn't interpret it.
    """
    return FixtureCase(
        id=str(message.id),
        band="live",
        lang=message.language or "und",
        channel=channel_type,
        party=party_display_name,
        channel_capability=channel_capability,
        sent_at=message.sent_at.isoformat(),
        message=message.text or "",
        sent_weekday=message.sent_at.strftime("%A"),
    )
