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
from sqlalchemy import select

from app.capture.adapters.registry import get_adapter
from app.capture.pipeline import IngestionSummary, ingest_channel_backlog
from app.core.db import async_session_factory, org_scoped_transaction
from app.llm.client import ModelClient
from app.models import Channel, Project

logger = logging.getLogger("cue.capture.worker")

_JOB_NAME = "ingest_channel_job"


def _summary_dict(summary: IngestionSummary) -> dict:
    return {
        "received": summary.received,
        "new_messages": summary.new_messages,
        "duplicates": summary.duplicates,
        "opted_out": summary.opted_out,
        "commitments_created": summary.commitments_created,
        # created alone cannot tell "quiet week" from "the model collapsed
        # every new promise into an existing one" — see ExtractionCounts.
        "commitments_linked": summary.commitments_linked,
        "commitments_flagged": summary.commitments_flagged,
        "extractions_rejected": summary.extractions_rejected,
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
        async with org_scoped_transaction(session, org_uuid):
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

        # ingest_channel_backlog (app/capture/pipeline.py) re-asserts org
        # context itself, per message, via the same org_scoped_transaction
        # — not covered by the block above, which only scopes the reads.
        adapter = get_adapter(channel.type)
        summary = await ingest_channel_backlog(
            session, project=project, channel=channel, adapter=adapter, since=since_dt, client=client
        )
        return _summary_dict(summary)


def channel_job_id(channel_id: uuid.UUID) -> str:
    """The deterministic per-channel arq job id `enqueue_channel_ingestion`
    below keys its own dedup on — pulled out as its own function (not just
    an inline f-string at each call site) so a status lookup
    (`app/api/channels.py`'s `channel_capture_status`) can construct the
    exact same id independently, without the two places drifting apart."""
    return f"ingest-channel-{channel_id}"


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
        _job_id=channel_job_id(channel_id),
    )
