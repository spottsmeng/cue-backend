"""FR-CAP-10 (Should): app/capture/schedule.py — scheduled extraction
windows. Real Postgres rows, real FixtureAdapter-driven backlog fetch
(scripted LLM client, same posture as every other pipeline-touching test in
this suite).
"""

from datetime import datetime, timedelta, timezone as dt_timezone

import pytest
from sqlalchemy import select

from app.capture.models import ChannelExtractionSchedule, Message
from app.capture.schedule import _is_due, run_due_extraction_schedules
from app.models import Channel
from tests.conftest import set_org_context


class _NoCommitmentsClient:
    async def complete(self, prompt: str, schema: dict) -> str:
        return '{"commitments": []}'


def _config(interval_minutes=30, last_run_at=None, active=True):
    return ChannelExtractionSchedule(
        interval_minutes=interval_minutes, last_run_at=last_run_at, active=active,
    )


def test_is_due_when_never_run():
    assert _is_due(_config(last_run_at=None), datetime.now(dt_timezone.utc)) is True


def test_is_due_false_within_interval():
    now = datetime.now(dt_timezone.utc)
    config = _config(interval_minutes=30, last_run_at=now - timedelta(minutes=10))
    assert _is_due(config, now) is False


def test_is_due_true_past_interval():
    now = datetime.now(dt_timezone.utc)
    config = _config(interval_minutes=30, last_run_at=now - timedelta(minutes=45))
    assert _is_due(config, now) is True


@pytest.mark.asyncio
async def test_run_due_extraction_schedules_ingests_and_updates_last_run(app_session, authed_org_and_project):
    org_id, project_id, user, _token = authed_org_and_project
    await set_org_context(app_session, org_id)
    channel = Channel(project_id=project_id, type="whatsapp", external_ref="g1", healthy=True)
    app_session.add(channel)
    await app_session.flush()
    schedule = ChannelExtractionSchedule(
        project_id=project_id, channel_id=channel.id, interval_minutes=15, active=True,
        created_by=user.id,
    )
    app_session.add(schedule)
    await app_session.commit()

    ran = await run_due_extraction_schedules(client=_NoCommitmentsClient())
    assert ran >= 1

    await app_session.refresh(schedule)
    assert schedule.last_run_at is not None

    messages = (
        await app_session.execute(select(Message).where(Message.channel_id == channel.id))
    ).scalars().all()
    assert len(messages) == 6  # every whatsapp case in cue-eval/cases.json


@pytest.mark.asyncio
async def test_run_due_extraction_schedules_skips_inactive(app_session, authed_org_and_project):
    org_id, project_id, user, _token = authed_org_and_project
    await set_org_context(app_session, org_id)
    channel = Channel(project_id=project_id, type="whatsapp", external_ref="g1", healthy=True)
    app_session.add(channel)
    await app_session.flush()
    schedule = ChannelExtractionSchedule(
        project_id=project_id, channel_id=channel.id, interval_minutes=15, active=False,
        created_by=user.id,
    )
    app_session.add(schedule)
    await app_session.commit()

    ran = await run_due_extraction_schedules(client=_NoCommitmentsClient())
    assert ran == 0

    messages = (
        await app_session.execute(select(Message).where(Message.channel_id == channel.id))
    ).scalars().all()
    assert messages == []


@pytest.mark.asyncio
async def test_run_due_extraction_schedules_skips_not_yet_due(app_session, authed_org_and_project):
    org_id, project_id, user, _token = authed_org_and_project
    await set_org_context(app_session, org_id)
    channel = Channel(project_id=project_id, type="whatsapp", external_ref="g1", healthy=True)
    app_session.add(channel)
    await app_session.flush()
    schedule = ChannelExtractionSchedule(
        project_id=project_id, channel_id=channel.id, interval_minutes=30,
        last_run_at=datetime.now(dt_timezone.utc), active=True, created_by=user.id,
    )
    app_session.add(schedule)
    await app_session.commit()

    ran = await run_due_extraction_schedules(client=_NoCommitmentsClient())
    assert ran == 0
