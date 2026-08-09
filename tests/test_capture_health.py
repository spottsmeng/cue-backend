"""FR-CAP-09: app/capture/health.py — the active half of channel health.
Real: writes the durable ChannelHealthEvent row and the live Channel.healthy
flag against real Postgres (same posture as every other test in this
suite); the only thing faked is the adapter's own health() response, since
proving a genuinely degraded network condition end to end isn't something a
unit test can manufacture — that's tests/test_capture_adapters_live.py's
job for the adapters that have real infrastructure to check against.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.capture.health import check_channel_health, run_capture_health_sweep
from app.capture.models import ChannelHealthEvent
from app.capture.schema import ChannelHealthResult
from app.models import Channel
from main import app
from tests.conftest import set_org_context


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class _FakeAdapter:
    def __init__(self, result: ChannelHealthResult | None = None, raises: bool = False):
        self._result = result
        self._raises = raises

    async def health(self, channel: Channel) -> ChannelHealthResult:
        if self._raises:
            raise RuntimeError("simulated network failure")
        return self._result


@pytest.mark.asyncio
async def test_check_channel_health_records_healthy_event(app_session, org_and_project, monkeypatch):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    channel = Channel(project_id=project_id, type="whatsapp", external_ref="g1", healthy=True)
    app_session.add(channel)
    await app_session.commit()

    monkeypatch.setattr(
        "app.capture.health.get_adapter",
        lambda code: _FakeAdapter(ChannelHealthResult(healthy=True, detail={"ping": "ok"})),
    )

    event = await check_channel_health(app_session, channel=channel)
    await app_session.commit()

    assert event.healthy is True
    assert event.detail == {"ping": "ok"}
    assert channel.healthy is True

    rows = (
        await app_session.execute(select(ChannelHealthEvent).where(ChannelHealthEvent.channel_id == channel.id))
    ).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_check_channel_health_marks_degraded_and_logs(
    app_session, org_and_project, monkeypatch
):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    channel = Channel(project_id=project_id, type="whatsapp", external_ref="g1", healthy=True)
    app_session.add(channel)
    await app_session.commit()

    monkeypatch.setattr(
        "app.capture.health.get_adapter",
        lambda code: _FakeAdapter(ChannelHealthResult(healthy=False, detail={"error": "timeout"})),
    )
    logged: list[str] = []
    monkeypatch.setattr(
        "app.capture.health.logger.error", lambda msg, *args, **kwargs: logged.append(msg % args)
    )

    event = await check_channel_health(app_session, channel=channel)
    await app_session.commit()

    assert event.healthy is False
    assert channel.healthy is False
    assert any("CAPTURE HEALTH DEGRADED" in m for m in logged)


@pytest.mark.asyncio
async def test_check_channel_health_treats_adapter_exception_as_unhealthy(
    app_session, org_and_project, monkeypatch
):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    channel = Channel(project_id=project_id, type="whatsapp", external_ref="g1", healthy=True)
    app_session.add(channel)
    await app_session.commit()

    monkeypatch.setattr("app.capture.health.get_adapter", lambda code: _FakeAdapter(raises=True))

    event = await check_channel_health(app_session, channel=channel)
    await app_session.commit()

    assert event.healthy is False
    assert "error" in (event.detail or {})
    assert channel.healthy is False


@pytest.mark.asyncio
async def test_run_capture_health_sweep_checks_every_active_channel(app_session, org_and_project):
    """No monkeypatching here — exercises the real registry (fixture
    backend, always healthy) end to end across the discovery query."""
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    channel = Channel(project_id=project_id, type="whatsapp", external_ref="g1", healthy=True)
    app_session.add(channel)
    await app_session.commit()

    checked = await run_capture_health_sweep()
    assert checked >= 1


@pytest.mark.asyncio
async def test_health_history_api_returns_recorded_events(authed_org_and_project, monkeypatch):
    org_id, project_id, _admin, token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create = await client.post(
            f"/projects/{project_id}/channels",
            headers=_headers(token),
            json={"type": "whatsapp", "external_ref": "g1"},
        )
        assert create.status_code == 201, create.text
        channel_id = create.json()["id"]

    from app.core.db import async_session_factory
    from tests.conftest import set_org_context as _set_ctx

    async with async_session_factory() as session:
        await _set_ctx(session, org_id)
        channel = (await session.execute(select(Channel).where(Channel.id == uuid.UUID(channel_id)))).scalar_one()
        await check_channel_health(session, channel=channel)
        await session.commit()

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        history = await client.get(
            f"/projects/{project_id}/channels/{channel_id}/health/history", headers=_headers(token)
        )
    assert history.status_code == 200
    body = history.json()
    assert len(body) == 1
    assert body[0]["channel_id"] == channel_id
