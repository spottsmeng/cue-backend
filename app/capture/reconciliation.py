"""FR-CAP-08 (PRD §6.1): "Detect and recover capture gaps (agent restart,
session expiry, subscription lapse) by backfilling from the channel's own
history where the channel permits." A reconciliation job (arq-scheduled)
detects gaps by comparing expected vs. received message sequence per
channel, and triggers backfill — Prompt 11's own wording, implemented here
as: expected cadence is this channel's own recent inter-arrival baseline
(the same "compare against the entity's own history, not a fixed global
number" idiom app/foresight/silence.py's Silence Radar already establishes
for vendor response times, applied here at the channel-transport layer
instead of the commitment layer), and backfill is a call into
app/capture/pipeline.py's own ingest_channel_backlog — "each adapter's
fetch_backlog() is the backfill path" is Prompt 11's own instruction, not a
separate recovery mechanism this module invents.
"""

import logging
import statistics
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.capture.adapters.registry import get_adapter
from app.capture.models import Message
from app.capture.pipeline import IngestionSummary, ingest_channel_backlog
from app.core.config import get_settings
from app.core.db import async_session_factory
from app.llm.client import ModelClient
from app.models import Channel, Project

logger = logging.getLogger("cue.capture.reconciliation")

# A gap has to be at least this long before it's worth a backfill call at
# all — a channel that normally goes quiet for stretches (e.g. an
# imap_smtp mailbox between business days) shouldn't trigger a
# reconciliation pass every single sweep. Same "documented starting point,
# not tuned against real traffic" posture as every other threshold in this
# codebase's scheduled jobs.
_MIN_GAP = timedelta(hours=1)
# How many multiples of the channel's own established baseline gap counts
# as "anomalously overdue" — mirrors the shape of Silence Radar's own
# multiplier-over-baseline detection (app/foresight/silence.py), a
# different entity (channel cadence, not vendor response time) but the
# same statistical idiom.
_GAP_MULTIPLIER = 3.0
# How many of the channel's most recent messages establish its baseline
# cadence — enough to be a real median, not a fluke of the last two
# messages happening to arrive close together.
_BASELINE_SAMPLE_SIZE = 10
# How far back of an overlap a triggered backfill re-fetches from, when no
# real baseline exists yet (a channel with 0 or 1 messages ever) — a
# reasonable "hasn't captured anything for a day, go back a week" default,
# not a tuned number.
_DEFAULT_BACKFILL_WINDOW = timedelta(days=7)


@dataclass
class GapDetectionResult:
    has_gap: bool
    last_message_at: datetime | None
    baseline_gap: timedelta | None
    overdue_by: timedelta | None


async def _recent_sent_ats(session: AsyncSession, channel_id: uuid.UUID) -> list[datetime]:
    stmt = (
        select(Message.sent_at)
        .where(Message.channel_id == channel_id)
        .order_by(Message.sent_at.desc())
        .limit(_BASELINE_SAMPLE_SIZE)
    )
    return list((await session.execute(stmt)).scalars().all())


async def detect_gap(session: AsyncSession, *, channel: Channel, now: datetime | None = None) -> GapDetectionResult:
    """FR-CAP-08's "compare expected vs. received message sequence" —
    "expected" is this channel's own recent cadence (median gap between its
    last `_BASELINE_SAMPLE_SIZE` messages), not a fixed number every
    channel is held to; a busy team_collaboration channel and a
    once-a-week email thread have structurally different "normal", the
    same reasoning Silence Radar's own baseline is per-vendor, not global.

    No real baseline yet (fewer than 2 messages ever captured on this
    channel) is reported as no gap — there is nothing to compare against,
    and a freshly-attached channel legitimately has no history; item 10's
    own backfill still runs for a brand-new channel via
    ingest_channel_backlog's `since=None` full-backlog fetch the first time
    it's ever ingested (item 3/11's job), not this detector's.
    """
    now = now or datetime.now(dt_timezone.utc)
    recent = await _recent_sent_ats(session, channel.id)
    if len(recent) < 2:
        return GapDetectionResult(has_gap=False, last_message_at=recent[0] if recent else None,
                                   baseline_gap=None, overdue_by=None)

    deltas = [(recent[i] - recent[i + 1]).total_seconds() for i in range(len(recent) - 1)]
    baseline_seconds = statistics.median(deltas)
    baseline_gap = timedelta(seconds=baseline_seconds)

    last_message_at = recent[0]
    since_last = now - last_message_at
    threshold = max(baseline_gap * _GAP_MULTIPLIER, _MIN_GAP)

    has_gap = since_last > threshold
    return GapDetectionResult(
        has_gap=has_gap, last_message_at=last_message_at, baseline_gap=baseline_gap,
        overdue_by=(since_last - threshold) if has_gap else None,
    )


async def backfill_channel(
    session: AsyncSession,
    *,
    project: Project,
    channel: Channel,
    gap: GapDetectionResult,
    client: ModelClient | None = None,
) -> IngestionSummary:
    """FR-CAP-08's recovery half — "each adapter's fetch_backlog() is the
    backfill path" (Prompt 11's own instruction): this is a plain call into
    app/capture/pipeline.py's ingest_channel_backlog, the same function
    item 3's queue and item 11's scheduled windows already use, with
    `since` set far enough back to genuinely re-cover the gap. Safe to
    re-run even when nothing was actually missed — every message it
    re-fetches that's already captured is a no-op via payload_hash dedup
    (FR-CAP-11, app/capture/normalise.py).
    """
    if gap.last_message_at is not None and gap.baseline_gap is not None:
        since = gap.last_message_at - gap.baseline_gap
    elif gap.last_message_at is not None:
        since = gap.last_message_at - _DEFAULT_BACKFILL_WINDOW
    else:
        since = None  # never captured anything on this channel — full backlog
    adapter = get_adapter(channel.type)
    logger.warning(
        "FR-CAP-08 gap detected: channel=%s type=%s last_message_at=%s overdue_by=%s — backfilling since=%s",
        channel.id, channel.type, gap.last_message_at, gap.overdue_by, since,
    )
    return await ingest_channel_backlog(
        session, project=project, channel=channel, adapter=adapter, since=since, client=client
    )


async def _discover_active_channels() -> list[tuple[uuid.UUID, uuid.UUID]]:
    """Same RLS-bypass discovery shape app/capture/health.py's own
    _discover_active_channels and app/foresight/worker.py's
    _discover_active_projects already establish."""
    engine = create_async_engine(get_settings().migration_database_url)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT p.organisation_id, c.id FROM channels c "
                        "JOIN projects p ON p.id = c.project_id WHERE p.archived_at IS NULL"
                    )
                )
            ).all()
            return [(row.organisation_id, row.id) for row in rows]
    finally:
        await engine.dispose()


async def run_gap_reconciliation_sweep(ctx: dict | None = None) -> int:
    """The arq cron job body — also directly callable, same shape as
    app/capture/health.py's run_capture_health_sweep. Returns the number of
    channels a gap was detected (and a backfill triggered) for."""
    targets = await _discover_active_channels()
    backfilled = 0
    for organisation_id, channel_id in targets:
        async with async_session_factory() as session:
            await session.execute(
                text("SELECT set_config('app.current_org_id', :oid, true)"), {"oid": str(organisation_id)}
            )
            channel = (
                await session.execute(select(Channel).where(Channel.id == channel_id))
            ).scalar_one_or_none()
            if channel is None:
                continue
            project = (
                await session.execute(select(Project).where(Project.id == channel.project_id))
            ).scalar_one_or_none()
            if project is None:
                continue
            try:
                gap = await detect_gap(session, channel=channel)
                if gap.has_gap:
                    await backfill_channel(session, project=project, channel=channel, gap=gap)
                    await session.commit()
                    backfilled += 1
            except Exception:
                await session.rollback()
                logger.exception("gap reconciliation failed for channel %s", channel_id)
    return backfilled
