"""app/capture/worker.py — `ingest_channel_job`, exercised as a plain
callable against the real test database (same "no queue-specific ctx
dependency" posture tests/test_foresight_worker.py already establishes for
run_foresight_sweep), plus `enqueue_channel_ingestion`'s per-channel
dedup-by-job-id guarantee against the real Valkey container docker-compose
already runs for this codebase (CUE_ARQ_* defaults to localhost:6379,
docker-compose.yml's `valkey` service) — the actual mechanism item 3's own
"per-channel ordered queue" requirement rests on, not something a fake
Redis client could prove.
"""

import uuid

import pytest
from arq.connections import RedisSettings, create_pool
from sqlalchemy import select

from app.capture.worker import enqueue_channel_ingestion, ingest_channel_job
from app.foresight.config import get_arq_settings
from app.models import Channel, Commitment
from tests.conftest import set_org_context


class _NoCommitmentsClient:
    """A fast, deterministic fake — same "never let a test depend on a live
    Ollama" boundary tests/test_extractor.py's own FakeModelClient already
    establishes (and cue-eval/README.md's own note that the live-model path
    is deliberately excluded from this automated suite). `client` is a
    keyword-only test seam on ingest_channel_job specifically so this
    module's own tests don't have to reach for that live path at all."""

    async def complete(self, prompt: str, schema: dict) -> str:
        return '{"commitments": []}'


@pytest.mark.asyncio
async def test_ingest_channel_job_processes_real_backlog(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    channel = Channel(project_id=project_id, type="whatsapp", external_ref="g1", healthy=True)
    app_session.add(channel)
    await app_session.commit()
    channel_id = channel.id

    result = await ingest_channel_job(
        None, str(org_id), str(channel_id), client=_NoCommitmentsClient()
    )

    assert result["received"] == 6
    assert result["new_messages"] == 6
    assert result["commitments_created"] == 0
    commitments = (
        await app_session.execute(select(Commitment).where(Commitment.project_id == project_id))
    ).scalars().all()
    assert commitments == []


@pytest.mark.asyncio
async def test_ingest_channel_job_reports_missing_channel_without_raising(org_and_project):
    org_id, _project_id = org_and_project
    result = await ingest_channel_job(None, str(org_id), str(uuid.uuid4()))
    assert result == {"skipped": True, "reason": "channel not found"}


@pytest.mark.asyncio
async def test_enqueue_is_deduplicated_per_channel_against_real_valkey(org_and_project):
    """The core new guarantee this module adds: a second enqueue for a
    channel that already has a job queued is a no-op (arq's own _job_id
    semantics), not a second concurrent job — which is what makes
    app/capture/pipeline.py's in-order fetch_backlog consumption a genuine
    per-channel ordering guarantee rather than an accident of timing."""
    org_id, _project_id = org_and_project
    channel_id = uuid.uuid4()
    settings = get_arq_settings()
    redis = await create_pool(RedisSettings(host=settings.redis_host, port=settings.redis_port))
    try:
        # Clear any stale job with this id from a previous failed test run.
        await redis.delete(f"arq:job:ingest-channel-{channel_id}")

        first = await enqueue_channel_ingestion(redis, organisation_id=org_id, channel_id=channel_id)
        second = await enqueue_channel_ingestion(redis, organisation_id=org_id, channel_id=channel_id)

        assert first is not None
        assert first.job_id == f"ingest-channel-{channel_id}"
        assert second is None  # deduplicated — no worker is running to drain the first job

        # Clean up so this job doesn't linger in the real queue for whatever
        # picks it up next (no worker process runs during the test suite).
        await redis.delete(f"arq:job:ingest-channel-{channel_id}")
        await redis.zrem("arq:queue", first.job_id)
    finally:
        await redis.aclose()
