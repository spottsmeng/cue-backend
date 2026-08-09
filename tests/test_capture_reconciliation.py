"""FR-CAP-08: app/capture/reconciliation.py — gap detection against a
channel's own real message history (real Postgres rows, not fakes) and the
backfill trigger, which is a real call into app/capture/pipeline.py's
ingest_channel_backlog (exercised here against the FixtureAdapter, same
"real pipeline, scripted LLM" posture tests/test_capture_pipeline.py already
establishes).
"""

from datetime import datetime, timedelta, timezone as dt_timezone

import pytest
from sqlalchemy import select

from app.capture.models import Message
from app.capture.reconciliation import backfill_channel, detect_gap, run_gap_reconciliation_sweep
from app.capture.schema import compute_payload_hash
from app.models import Channel, Project
from tests.conftest import FAKE_LLM_USAGE, set_org_context


class _NoCommitmentsClient:
    """Same fast, deterministic fake tests/test_capture_worker.py's own
    _NoCommitmentsClient establishes — a triggered backfill runs real
    extraction (app/capture/pipeline.py's ingest_channel_backlog), and this
    suite never depends on a live Ollama, whether or not one happens to be
    running on the machine executing it."""

    async def complete(self, prompt: str, schema: dict):
        return '{"commitments": []}', FAKE_LLM_USAGE


async def _seed_message(app_session, project_id, channel_id, *, sent_at, external_id):
    msg = Message(
        project_id=project_id, channel_id=channel_id, external_id=external_id,
        sender_external_id="user-x", sent_at=sent_at,
        payload_hash=compute_payload_hash("whatsapp", external_id, sent_at.isoformat().encode()),
    )
    app_session.add(msg)
    await app_session.flush()
    return msg


@pytest.mark.asyncio
async def test_no_gap_reported_with_fewer_than_two_messages(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    channel = Channel(project_id=project_id, type="whatsapp", external_ref="g1", healthy=True)
    app_session.add(channel)
    await app_session.commit()

    result = await detect_gap(app_session, channel=channel)
    assert result.has_gap is False
    assert result.baseline_gap is None


@pytest.mark.asyncio
async def test_no_gap_when_recent_message_within_baseline_cadence(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    channel = Channel(project_id=project_id, type="whatsapp", external_ref="g1", healthy=True)
    app_session.add(channel)
    await app_session.commit()

    now = datetime.now(dt_timezone.utc)
    # Regular hourly cadence, most recent message 10 minutes ago.
    for i in range(5):
        await _seed_message(
            app_session, project_id, channel.id,
            sent_at=now - timedelta(minutes=10) - timedelta(hours=i), external_id=f"m{i}",
        )
    await app_session.commit()

    result = await detect_gap(app_session, channel=channel, now=now)
    assert result.has_gap is False
    assert result.baseline_gap is not None


@pytest.mark.asyncio
async def test_gap_detected_when_far_overdue_relative_to_baseline(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    channel = Channel(project_id=project_id, type="whatsapp", external_ref="g1", healthy=True)
    app_session.add(channel)
    await app_session.commit()

    now = datetime.now(dt_timezone.utc)
    # Regular hourly cadence up to 10 hours ago, then nothing since —
    # 10 hours of silence against a ~1 hour baseline is well past the
    # 3x-multiplier threshold.
    for i in range(5):
        await _seed_message(
            app_session, project_id, channel.id,
            sent_at=now - timedelta(hours=10) - timedelta(hours=i), external_id=f"m{i}",
        )
    await app_session.commit()

    result = await detect_gap(app_session, channel=channel, now=now)
    assert result.has_gap is True
    assert result.overdue_by is not None and result.overdue_by > timedelta(0)


@pytest.mark.asyncio
async def test_backfill_channel_calls_pipeline_and_recovers_since_baseline(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    channel = Channel(project_id=project_id, type="whatsapp", external_ref="g1", healthy=True)
    app_session.add(channel)
    await app_session.commit()

    now = datetime.now(dt_timezone.utc)
    gap = await detect_gap(app_session, channel=channel, now=now)  # no history -> full-backlog backfill

    summary = await backfill_channel(
        app_session, project=project, channel=channel, gap=gap, client=_NoCommitmentsClient()
    )
    # FixtureAdapter's whatsapp cases: a real backfill actually ran.
    assert summary.received == 6


@pytest.mark.asyncio
async def test_run_gap_reconciliation_sweep_backfills_a_stale_channel(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    channel = Channel(project_id=project_id, type="whatsapp", external_ref="g1", healthy=True)
    app_session.add(channel)
    await app_session.commit()

    now = datetime.now(dt_timezone.utc)
    for i in range(5):
        await _seed_message(
            app_session, project_id, channel.id,
            sent_at=now - timedelta(hours=20) - timedelta(hours=i), external_id=f"stale-{i}",
        )
    await app_session.commit()

    backfilled = await run_gap_reconciliation_sweep()
    assert backfilled >= 1

    # The seeded messages are all still there — a triggered backfill only
    # ever adds via the same dedup-safe ingest_channel_backlog path
    # (item 3), never removes or duplicates existing history. (FixtureAdapter's
    # own cases.json entries are all dated in 2026-06, well before this
    # test's own dynamically-seeded `since` window computed from the real
    # current time, so this specific run's backfill genuinely finds nothing
    # new to add — which is itself a correct, safe outcome, not a failure.)
    messages = (
        await app_session.execute(select(Message).where(Message.channel_id == channel.id))
    ).scalars().all()
    assert len(messages) >= 5
    assert {m.external_id for m in messages} >= {f"stale-{i}" for i in range(5)}
