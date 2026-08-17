"""FR-CAP-10 (Should): "Support scheduled extraction windows in addition to
event-driven capture, configurable per channel." Mirrors
app/reports/schedule.py's own shape closely (same schema-owner discovery
bypass and reasoning, same directly-callable-without-a-broker contract) —
calls app/capture/pipeline.py's ingest_channel_backlog directly, the same
function item 3's queue and item 10's reconciliation job already use,
rather than a parallel ingestion path of its own. "In addition to (not
instead of) event-driven capture" (Prompt 11's own wording): this runs
regardless of whatever else already triggered ingestion for a channel —
ingest_channel_backlog's own payload_hash dedup (FR-CAP-11) is what makes
that safe, not any coordination this module does.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.capture.adapters.registry import get_adapter
from app.capture.models import ChannelExtractionSchedule
from app.capture.pipeline import ingest_channel_backlog
from app.core.config import get_settings
from app.core.db import async_session_factory, set_local_org_context
from app.llm.client import ModelClient
from app.models import Channel, Project

logger = logging.getLogger("cue.capture.schedule")


async def _discover_active_schedules() -> list[tuple[uuid.UUID, uuid.UUID]]:
    """(organisation_id, schedule_id) for every active schedule on a
    non-archived project's channel, across every tenant — same RLS-bypass
    discovery shape app/reports/schedule.py's own _discover_due_schedules
    already establishes, for the same reason (no service-account/agent
    identity exists yet to run a platform-level scan as)."""
    engine = create_async_engine(get_settings().migration_database_url)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT p.organisation_id, ces.id FROM channel_extraction_schedules ces "
                        "JOIN projects p ON p.id = ces.project_id "
                        "WHERE ces.active = true AND p.archived_at IS NULL"
                    )
                )
            ).all()
            return [(row.organisation_id, row.id) for row in rows]
    finally:
        await engine.dispose()


def _is_due(config: ChannelExtractionSchedule, now: datetime) -> bool:
    if config.last_run_at is None:
        return True
    return now - config.last_run_at >= timedelta(minutes=config.interval_minutes)


async def run_due_extraction_schedules(ctx: dict | None = None, *, client: ModelClient | None = None) -> int:
    """The arq cron job body — also directly callable (by tests, or a
    one-off ops invocation) without a running worker/broker, same contract
    app/reports/schedule.py's run_due_report_schedules already establishes.
    Returns the number of channels a scheduled window actually ran for.

    `client` is a keyword-only test seam (never set by arq's own dispatch,
    which only ever passes `ctx`) — same reasoning
    app/capture/worker.py's ingest_channel_job already documents for its
    own `client` parameter.
    """
    targets = await _discover_active_schedules()
    now = datetime.now(dt_timezone.utc)
    ran = 0

    for organisation_id, schedule_id in targets:
        async with async_session_factory() as session:
            await set_local_org_context(session, organisation_id)
            config = (
                await session.execute(
                    select(ChannelExtractionSchedule).where(ChannelExtractionSchedule.id == schedule_id)
                )
            ).scalar_one_or_none()
            if config is None or not config.active or not _is_due(config, now):
                continue

            channel = (
                await session.execute(select(Channel).where(Channel.id == config.channel_id))
            ).scalar_one_or_none()
            project = (
                await session.execute(select(Project).where(Project.id == config.project_id))
            ).scalar_one_or_none()
            # A detached channel's row survives (soft-delete, see
            # app/api/channels.py's detach_channel) but its Layer A capture
            # permission was already revoked at detach time — running a
            # scheduled pull against it would just fail against Layer A (or
            # worse, silently keep capturing if the revoke hadn't yet taken
            # effect), for a channel an admin deliberately turned off.
            if channel is None or channel.detached_at is not None or project is None:
                continue

            try:
                adapter = get_adapter(channel.type)
                await ingest_channel_backlog(
                    session, project=project, channel=channel, adapter=adapter, since=config.last_run_at,
                    client=client,
                )
                # ingest_channel_backlog re-asserts org context itself, per
                # message, but its own last commit (inside that call)
                # already released the connection back to the pool — this
                # statement starts a fresh transaction and needs its own
                # re-assertion, not a leftover from the call above.
                await set_local_org_context(session, organisation_id)
                config.last_run_at = now
                await session.commit()
                ran += 1
            except Exception:
                await session.rollback()
                logger.exception("scheduled extraction window failed for channel %s", config.channel_id)

    return ran
