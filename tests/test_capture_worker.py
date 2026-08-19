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

import json
import uuid

import pytest
from arq.connections import RedisSettings, create_pool
from sqlalchemy import select

from app.capture.models import Message, MessageMedia
from app.capture.worker import enqueue_channel_ingestion, ingest_channel_job
from app.foresight.config import get_arq_settings
from app.models import Channel, Commitment, Evidence
from tests.conftest import FAKE_LLM_USAGE, set_org_context


class _NoCommitmentsClient:
    """A fast, deterministic fake — same "never let a test depend on a live
    Ollama" boundary tests/test_extractor.py's own FakeModelClient already
    establishes (and cue-eval/README.md's own note that the live-model path
    is deliberately excluded from this automated suite). `client` is a
    keyword-only test seam on ingest_channel_job specifically so this
    module's own tests don't have to reach for that live path at all."""

    async def complete(self, prompt: str, schema: dict):
        return '{"commitments": []}', FAKE_LLM_USAGE


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


class _CommitmentFromVoiceNoteClient:
    """Real ASR (no override — get_default_asr_client's own FasterWhisper
    "tiny" model, already proven fast/deterministic enough for this exact
    fixture, tests/test_capture_voice_note_pipeline.py's own real-ASR
    precedent) produces the transcript this rule matches against; only the
    extraction call itself is scripted, same "one thing not proven live"
    boundary every other test in this module already draws."""

    async def complete(self, prompt: str, schema: dict):
        if "LED wall installation" in prompt:
            return (
                json.dumps(
                    {
                        "commitments": [
                            {
                                "act_type": "commit",
                                "deliverable_en": "LED wall installation",
                                "deliverable_original": "LED wall installation",
                                "evidence_span": "LED wall installation will be completed by Friday",
                                "confidence": 0.9,
                            }
                        ]
                    }
                ),
                FAKE_LLM_USAGE,
            )
        return '{"commitments": []}', FAKE_LLM_USAGE


@pytest.mark.asyncio
async def test_voice_demo_channel_processes_the_real_fixture_voice_note(app_session, org_and_project):
    """Blind Spots follow-up (TC-05): a WeChat channel attached with
    external reference "voice-demo" gets FixtureAdapter's own real
    voice-note fixture — real checked-in audio, real ASR, real extraction,
    through the exact same `ingest_channel_job` a live "Pull now" click
    calls. WeChat, not WhatsApp — attaching a `whatsapp` channel through the
    real product replaces the free-text external-reference field with a
    real conversation picker (the Layer B Channel Picker work), so a typed
    sentinel like this one is only actually reachable for a channel type
    that keeps the plain field. Closes the "transcript_confidence has no
    path to exist outside a live channel" gap named in frontend/
    PROGRESS.md's Blind Spots notes — this is what makes that path real
    without needing one.
    """
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    channel = Channel(project_id=project_id, type="wechat", external_ref="voice-demo", healthy=True)
    app_session.add(channel)
    await app_session.commit()
    channel_id = channel.id

    result = await ingest_channel_job(
        None, str(org_id), str(channel_id), client=_CommitmentFromVoiceNoteClient()
    )

    # A "voice-demo" wechat channel still gets cases.json's own 4 text
    # cases (unchanged from every other wechat channel) plus this one real
    # voice note — 5, not 1.
    assert result["received"] == 5
    assert result["new_messages"] == 5
    assert result["commitments_created"] == 1

    message = (
        await app_session.execute(select(Message).where(Message.channel_id == channel_id, Message.external_id == "VN01"))
    ).scalar_one()
    # The real transcript, copied onto Message.text since this message
    # arrived with no text of its own (media_pipeline.py's own voice-note
    # posture) — proves ASR genuinely ran, not a canned string.
    assert message.text is not None
    assert "LED wall installation" in message.text
    assert message.language == "en"

    media = (
        await app_session.execute(select(MessageMedia).where(MessageMedia.message_id == message.id))
    ).scalar_one()
    assert media.kind == "voice_note"
    assert media.storage_key is not None
    assert media.transcript_confidence is not None
    assert 0.0 <= media.transcript_confidence <= 1.0

    commitment = (
        await app_session.execute(select(Commitment).where(Commitment.project_id == project_id))
    ).scalar_one()
    evidence = (
        await app_session.execute(select(Evidence).where(Evidence.commitment_id == commitment.id))
    ).scalar_one()
    # Item 5's own real, end-to-end surface: the same real confidence the
    # media pipeline computed, now on the Evidence row EvidenceViewer reads.
    assert evidence.media_ref is not None
    assert evidence.transcript_confidence == media.transcript_confidence


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
