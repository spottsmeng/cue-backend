"""Item 3's arq job — arq is already in this codebase (added by the
Foresight session, M4, CUE-Tech-Stack.md §2.4: "Lightweight task queue —
arq (Valkey-backed)") — reused here, not stood up a second time, per Prompt
11b's own instruction. `ingest_channel_job` is the one unit of queued work
every trigger (item 10's gap/backfill, item 11's scheduled windows, or a
manual "poll now" admin action) enqueues; app/foresight/worker.py's shared
WorkerSettings is what actually runs it, the same "ride the one background-
job process this codebase runs" precedent that module's own docstring
already establishes for app/reports/schedule.py and app/ask/embed_worker.py.

**Per-channel ordering, without a per-channel arq queue.** arq's own queue
model is one flat queue per Redis/Valkey instance — there is no first-class
"ordered sub-queue per key" primitive the way the architecture doc's
aspirational pgmq sketch implies (CUE-Tech-Stack.md §3's diagram). What this
module builds instead: `enqueue_channel_ingestion` passes a `_job_id` derived
from the channel id, and arq's own job-id semantics make a second enqueue
for a channel that already has one queued or running a no-op (returns None,
not a second job) — at most one ingestion job in flight per channel at any
time. Combined with app/capture/pipeline.py's own in-order `fetch_backlog`
consumption *within* that one job, this is a real ordering guarantee: two
messages from the same channel are never normalised/identity-resolved out of
order, even though two *different* channels' jobs run concurrently.
**At-least-once.** arq retries a job that raises past its own attempt limit
(arq's default job behaviour) — safe here because every layer underneath is
independently idempotent: normalise_and_ingest's payload_hash dedup
(FR-CAP-11) and extract_from_message's `extraction_attempted_at` guard both
make a full re-run of an already-processed message a no-op, not a duplicate.
"""

import logging
import uuid
from datetime import datetime, timezone as dt_timezone

from arq.connections import ArqRedis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.capture.adapters.registry import get_adapter
from app.capture.pipeline import IngestionSummary, ingest_channel_backlog
from app.core.db import async_session_factory
from app.llm.client import ModelClient
from app.models import Channel, Project

logger = logging.getLogger("cue.capture.worker")

_JOB_NAME = "ingest_channel_job"


async def _set_org_context(session: AsyncSession, org_id: uuid.UUID) -> None:
    """Same `is_local=false` (session-wide, not per-statement) shape
    app/foresight/worker.py's own `_set_org_context` uses — one arq job
    processes one channel's whole backlog across many sequential commits
    (app/capture/pipeline.py's `ingest_channel_backlog` commits per
    message), and the org context has to survive every one of them."""
    await session.execute(
        text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)}
    )


def _summary_dict(summary: IngestionSummary) -> dict:
    return {
        "received": summary.received,
        "new_messages": summary.new_messages,
        "duplicates": summary.duplicates,
        "opted_out": summary.opted_out,
        "commitments_created": summary.commitments_created,
        "latest_sent_at": summary.latest_sent_at.isoformat() if summary.latest_sent_at else None,
    }


async def ingest_channel_job(
    ctx: dict | None,
    organisation_id: str,
    channel_id: str,
    since: str | None = None,
    *,
    client: ModelClient | None = None,
) -> dict:
    """The arq job body — also directly callable (by tests, or a one-off ops
    invocation) without a running worker/broker, since it takes no
    queue-specific state from `ctx`, the same shape
    app/foresight/worker.py's own `run_foresight_sweep` already establishes.
    `since` is an ISO timestamp (arq job args must be JSON-serialisable, so
    this takes strings, not uuid.UUID/datetime, at the boundary) —
    None means "the adapter's own full backlog", same as
    ChannelAdapter.fetch_backlog's own `since=None` convention.

    `client` defaults to None (app/capture/pipeline.py's own
    extract_case-configured default, Ollama/Anthropic per .env) — a
    keyword-only test seam, never set by a real `enqueue_channel_ingestion`
    call (arq only passes the positional args it was given), so this
    doesn't change production behaviour; it exists so tests never depend on
    a live Ollama, the same "not part of this automated suite on purpose"
    boundary cue-eval's own README already draws.
    """
    org_uuid = uuid.UUID(organisation_id)
    channel_uuid = uuid.UUID(channel_id)
    since_dt = datetime.fromisoformat(since) if since else None

    async with async_session_factory() as session:
        await _set_org_context(session, org_uuid)
        channel = (
            await session.execute(select(Channel).where(Channel.id == channel_uuid))
        ).scalar_one_or_none()
        if channel is None:
            logger.warning("ingest_channel_job: channel %s not found (deleted?)", channel_id)
            return {"skipped": True, "reason": "channel not found"}
        project = (
            await session.execute(select(Project).where(Project.id == channel.project_id))
        ).scalar_one_or_none()
        if project is None:
            logger.warning("ingest_channel_job: project for channel %s not found", channel_id)
            return {"skipped": True, "reason": "project not found"}

        adapter = get_adapter(channel.type)
        summary = await ingest_channel_backlog(
            session, project=project, channel=channel, adapter=adapter, since=since_dt, client=client
        )
        return _summary_dict(summary)


async def enqueue_channel_ingestion(
    redis: ArqRedis, *, organisation_id: uuid.UUID, channel_id: uuid.UUID, since: datetime | None = None
):
    """Returns the enqueued arq Job, or None if a job for this channel is
    already queued/running (see this module's own docstring — that's the
    per-channel ordering guarantee, not a bug to work around)."""
    return await redis.enqueue_job(
        _JOB_NAME,
        str(organisation_id),
        str(channel_id),
        since.isoformat() if since else None,
        _job_id=f"ingest-channel-{channel_id}",
    )
