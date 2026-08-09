"""FR-CAP-09 (PRD §6.1): "Emit a capture-health signal per channel per
project; surface degraded channels to Administrators within 15 minutes."
Every adapter's health() already exists (item 2) and returns a
ChannelHealthResult — nothing called it. Prompt 11b's own framing: "the
active half of channel health... genuinely unbuilt... this item's actual
job."

Writes directly to Channel.healthy / app/capture/models.py's
ChannelHealthEvent rather than making an authenticated HTTP call to the
existing POST /channels/{id}/health receiving endpoint (app/api/channels.py)
— that endpoint's own module docstring already names the reason: "There is
no service-account/agent identity yet for a real capture agent to call the
health endpoint as... this is the placeholder until then." This module *is*
that eventual caller, running in-process (the same backend, not a separate
capture-agent service) — it performs the identical mutation the endpoint
would (`channel.healthy = result.healthy`), just without a pointless
self-authenticated HTTP round trip that has nowhere to get a valid token
from yet.
"""

import logging
import uuid
from datetime import datetime, timezone as dt_timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.capture.adapters.base import ChannelAdapter
from app.capture.adapters.registry import get_adapter
from app.capture.models import ChannelHealthEvent
from app.capture.schema import ChannelHealthResult
from app.core.config import get_settings
from app.core.db import async_session_factory
from app.models import Channel

logger = logging.getLogger("cue.capture.health")


async def _call_adapter_health(adapter: ChannelAdapter, channel: Channel) -> ChannelHealthResult:
    """A channel that "stops reporting healthy pings at all" (Prompt 11b's
    own FR-CAP-09 framing) includes an adapter call that raises outright,
    not only one that returns healthy=False — both are degradation
    signals, normalised to the same ChannelHealthResult shape here rather
    than letting an exception skip this channel's check entirely."""
    try:
        return await adapter.health(channel)
    except Exception as e:
        logger.warning("health() raised for channel=%s type=%s: %s", channel.id, channel.type, e)
        return ChannelHealthResult(healthy=False, detail={"error": str(e)})


async def check_channel_health(session: AsyncSession, *, channel: Channel) -> ChannelHealthEvent:
    """One channel, one check: calls its adapter's health(), updates the
    live Channel.healthy flag app/api/channels.py's existing endpoints
    already read/write, and appends a ChannelHealthEvent — the durable
    history that flag alone never kept."""
    adapter = get_adapter(channel.type)
    result = await _call_adapter_health(adapter, channel)
    now = datetime.now(dt_timezone.utc)

    was_healthy = channel.healthy
    channel.healthy = result.healthy
    await session.flush()

    event = ChannelHealthEvent(
        project_id=channel.project_id, channel_id=channel.id, healthy=result.healthy,
        detail=result.detail, checked_at=now,
    )
    session.add(event)
    await session.flush()

    if was_healthy and not result.healthy:
        # FR-CAP-09: "surface degraded channels to Administrators within 15
        # minutes" — run_capture_health_sweep below runs on the same
        # 15-minute cadence as every other scheduled sweep in this codebase
        # (app/foresight/worker.py's WorkerSettings), so reacting the moment
        # a transition is observed already meets the SLA on its own; this
        # is the reaction. Routed as a direct structured log/alert, not a
        # Foresight Notification — Notification's own schema requires a
        # risk/deviation/commitment subject (app/foresight/models.py's
        # notification_has_a_subject CHECK constraint), none of which a
        # channel-health event is. Prompt 11b's own instruction explicitly
        # names "a direct log/alert otherwise" as the accepted fallback
        # when Notification's shape doesn't fit a finding, rather than
        # widening Foresight's own RiskSource enum for this one caller.
        logger.error(
            "CAPTURE HEALTH DEGRADED (FR-CAP-09): project=%s channel=%s type=%s detail=%s "
            "— administrator attention required",
            channel.project_id, channel.id, channel.type, result.detail,
        )
    return event


async def _discover_active_channels() -> list[tuple[uuid.UUID, uuid.UUID]]:
    """Returns (organisation_id, channel_id) for every channel belonging to
    a non-archived project, across every tenant — the same RLS-bypass
    discovery shape app/foresight/worker.py's own _discover_active_projects
    already establishes (schema-owner connection, only for discovery; the
    actual health check itself still runs through the ordinary RLS-enforced
    app_session with that channel's own org context set)."""
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


async def run_capture_health_sweep(ctx: dict | None = None) -> int:
    """The arq cron job body — also directly callable (by tests, or a
    one-off ops invocation) without a running worker/broker, since it takes
    no queue-specific state from `ctx`, the same shape
    app/foresight/worker.py's own run_foresight_sweep already establishes.
    Returns the number of channels successfully checked."""
    targets = await _discover_active_channels()
    checked = 0
    for organisation_id, channel_id in targets:
        async with async_session_factory() as session:
            await session.execute(
                text("SELECT set_config('app.current_org_id', :oid, true)"), {"oid": str(organisation_id)}
            )
            channel = (
                await session.execute(select(Channel).where(Channel.id == channel_id))
            ).scalar_one_or_none()
            if channel is None:
                continue  # deleted between discovery and check — skip, not an error
            try:
                await check_channel_health(session, channel=channel)
                await session.commit()
                checked += 1
            except Exception:
                await session.rollback()
                logger.exception("capture health check failed for channel %s", channel_id)
    return checked
