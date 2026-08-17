"""REST endpoints for /projects/{id}/channels (PRD §11.2, FR-ADM-06/09):
attach, detach, health, reconnect — activating app/models/project.py's
Channel model, previously untouched by any endpoint.
"""

import uuid
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.capture.models import Message
from app.capture.schema import compute_payload_hash
from app.identity.config import get_identity_settings
from app.identity.models import Membership, User
from main import app
from tests.conftest import mint_token, set_org_context


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _member(app_session, org_id, project_id, role, granted_by):
    await set_org_context(app_session, org_id)
    subject = f"{role}-{uuid.uuid4()}"
    user = User(
        organisation_id=org_id,
        issuer=get_identity_settings().local_issuer,
        external_subject=subject,
        email=f"{subject}@example.test",
    )
    app_session.add(user)
    await app_session.flush()
    app_session.add(Membership(user_id=user.id, project_id=project_id, role=role, granted_by=granted_by))
    await app_session.commit()
    return user, mint_token(org_id, subject=subject, email=user.email)


@pytest.mark.asyncio
async def test_attach_channel(authed_org_and_project):
    """`type="wechat"` deliberately, not `"whatsapp"` — a hand-typed
    `external_ref` for `type="whatsapp"` now triggers a real Layer A
    allowlist grant (`app/api/channels.py`'s `_grant_whatsapp_allowlist`,
    'Layer B Channel Picker' prompt item 2), which would either hit this
    developer's real linked WhatsApp account with a nonsense JID or fail
    outright wherever Layer A isn't running — neither of which this
    generic "attach works" test should depend on. The WhatsApp-specific
    coupling has its own real-infra test: test_channels_whatsapp_live.py."""
    org_id, project_id, _admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/channels",
            headers=_headers(admin_token),
            json={"type": "wechat", "external_ref": "group-123"},
        )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["type"] == "wechat"
    assert body["external_ref"] == "group-123"
    assert body["healthy"] is True


@pytest.mark.asyncio
async def test_designer_cannot_attach_channel(app_session, authed_org_and_project):
    """FR-ADM-06's 'attach channels' step is part of project provisioning,
    gated ADMIN_ROLES same as app/api/projects.py's add_member."""
    org_id, project_id, admin, _admin_token = authed_org_and_project
    _designer, designer_token = await _member(app_session, org_id, project_id, "designer", admin.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/channels",
            headers=_headers(designer_token),
            json={"type": "whatsapp", "external_ref": "group-123"},
        )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_producer_can_attach_channel(app_session, authed_org_and_project):
    """ADMIN_ROLES = {"administrator", "producer"}."""
    org_id, project_id, admin, _admin_token = authed_org_and_project
    _producer, producer_token = await _member(app_session, org_id, project_id, "producer", admin.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/channels",
            headers=_headers(producer_token),
            json={"type": "teams", "external_ref": "channel-abc"},
        )
    assert response.status_code == 201, response.text


@pytest.mark.asyncio
async def test_attach_channel_with_unknown_type_422s(authed_org_and_project):
    """Replaces ChannelTypeLiteral's static closed set with a DB lookup
    (app/api/channels.py's _resolve_channel_type) — same client-visible 422
    a bad Literal value used to produce, now for a code no channel_types
    row exists for."""
    org_id, project_id, _admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/channels",
            headers=_headers(admin_token),
            json={"type": "telegram"},
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_attach_channel_with_manual_type_422s(authed_org_and_project):
    """'manual' is FR-LED-10's non-integration Evidence.channel value
    (capability=None) — a Channel resource can never be attached as
    'manual', the same carve-out ChannelTypeLiteral (minus "manual")
    enforced before it was replaced by _resolve_channel_type's
    require_capability=True check."""
    org_id, project_id, _admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/channels",
            headers=_headers(admin_token),
            json={"type": "manual"},
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_list_channels(authed_org_and_project):
    org_id, project_id, _admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            f"/projects/{project_id}/channels",
            headers=_headers(admin_token),
            json={"type": "whatsapp"},
        )
        await client.post(
            f"/projects/{project_id}/channels",
            headers=_headers(admin_token),
            json={"type": "wechat"},
        )
        response = await client.get(f"/projects/{project_id}/channels", headers=_headers(admin_token))

    assert response.status_code == 200
    assert len(response.json()) == 2


@pytest.mark.asyncio
async def test_detach_channel(authed_org_and_project):
    org_id, project_id, _admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            f"/projects/{project_id}/channels",
            headers=_headers(admin_token),
            json={"type": "outlook"},
        )
        channel_id = created.json()["id"]

        detach = await client.delete(
            f"/projects/{project_id}/channels/{channel_id}", headers=_headers(admin_token)
        )
        listing = await client.get(f"/projects/{project_id}/channels", headers=_headers(admin_token))

    assert detach.status_code == 204
    assert listing.json() == []


@pytest.mark.asyncio
async def test_detach_channel_with_captured_messages_soft_deletes(app_session, authed_org_and_project):
    """Regression test: a channel with real captured messages used to
    500 on detach (`session.delete(channel)` hit `messages_channel_id_fkey`,
    which has no `ON DELETE` action — deliberately, since `Evidence` may
    already cite one of those messages). Detach must now succeed by setting
    `detached_at` instead, leaving the channel and its messages intact but
    the channel excluded from the active list."""
    org_id, project_id, _admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            f"/projects/{project_id}/channels",
            headers=_headers(admin_token),
            json={"type": "outlook"},
        )
        channel_id = uuid.UUID(created.json()["id"])

        await set_org_context(app_session, org_id)
        message = Message(
            project_id=project_id,
            channel_id=channel_id,
            external_id="msg-1",
            sender_external_id="user-x",
            sent_at=datetime.now(timezone.utc),
            payload_hash=compute_payload_hash("outlook", "msg-1", b"hello"),
        )
        app_session.add(message)
        await app_session.commit()

        detach = await client.delete(
            f"/projects/{project_id}/channels/{channel_id}", headers=_headers(admin_token)
        )
        listing = await client.get(f"/projects/{project_id}/channels", headers=_headers(admin_token))

    assert detach.status_code == 204, detach.text
    assert listing.json() == []

    await set_org_context(app_session, org_id)
    persisted = (
        await app_session.execute(select(Message).where(Message.id == message.id))
    ).scalar_one_or_none()
    assert persisted is not None, "message must survive channel detach"


@pytest.mark.asyncio
async def test_health_signal_updates_healthy_flag(authed_org_and_project):
    """FR-ADM-09: the receiving end of a capture-health signal — nothing
    calls this yet in production, but the endpoint must accept and apply the
    payload now."""
    org_id, project_id, _admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            f"/projects/{project_id}/channels",
            headers=_headers(admin_token),
            json={"type": "sharepoint"},
        )
        channel_id = created.json()["id"]
        assert created.json()["healthy"] is True

        degraded = await client.post(
            f"/projects/{project_id}/channels/{channel_id}/health",
            headers=_headers(admin_token),
            json={"healthy": False, "detail": {"error": "session expired"}},
        )

    assert degraded.status_code == 200, degraded.text
    assert degraded.json()["healthy"] is False


@pytest.mark.asyncio
async def test_reconnect_marks_channel_healthy(authed_org_and_project):
    org_id, project_id, _admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            f"/projects/{project_id}/channels",
            headers=_headers(admin_token),
            json={"type": "wechat"},
        )
        channel_id = created.json()["id"]

        await client.post(
            f"/projects/{project_id}/channels/{channel_id}/health",
            headers=_headers(admin_token),
            json={"healthy": False},
        )
        reconnected = await client.post(
            f"/projects/{project_id}/channels/{channel_id}/reconnect", headers=_headers(admin_token)
        )

    assert reconnected.status_code == 200, reconnected.text
    assert reconnected.json()["healthy"] is True


@pytest.mark.asyncio
async def test_attach_whatsapp_channel_without_layer_a_configured_503s(authed_org_and_project, monkeypatch):
    """'Layer B Channel Picker' prompt: `_grant_whatsapp_allowlist`
    (app/api/channels.py) must surface a missing Layer A configuration as a
    real, visible 503 — never a "successful" attach that silently never
    granted capture permission, the exact failure mode this coupling
    exists to prevent. `WhatsAppSettings` reads `.env` directly (not
    `os.environ`, see test_capture_adapters_live.py's own comment on this),
    so simulating "not configured" means patching the settings getter
    `app/capture/adapters/whatsapp.py` actually calls, not `monkeypatch.
    delenv`."""
    from app.capture.config import WhatsAppSettings

    monkeypatch.setattr(
        "app.capture.adapters.whatsapp.get_whatsapp_settings",
        lambda: WhatsAppSettings(session_endpoint=None, api_token=None),
    )

    org_id, project_id, _admin, admin_token = authed_org_and_project
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/channels",
            headers=_headers(admin_token),
            json={"type": "whatsapp", "external_ref": "120363297803566811@g.us"},
        )
        assert response.status_code == 503, response.text

        # Confirm nothing was persisted either — a failed allowlist grant
        # must not leave a Channel row behind.
        listing = await client.get(f"/projects/{project_id}/channels", headers=_headers(admin_token))
    assert listing.json() == []


@pytest.mark.asyncio
async def test_whatsapp_channel_without_external_ref_skips_allowlist_call(authed_org_and_project, monkeypatch):
    """An `external_ref`-less WhatsApp attach (nothing chosen from the
    picker yet) must not attempt an allowlist grant at all — confirmed by
    pointing WhatsApp settings at something that would error if called."""
    from app.capture.config import WhatsAppSettings

    monkeypatch.setattr(
        "app.capture.adapters.whatsapp.get_whatsapp_settings",
        lambda: WhatsAppSettings(session_endpoint=None, api_token=None),
    )

    org_id, project_id, _admin, admin_token = authed_org_and_project
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/channels",
            headers=_headers(admin_token),
            json={"type": "whatsapp"},
        )

    assert response.status_code == 201, response.text


@pytest.mark.asyncio
async def test_detach_whatsapp_channel_without_layer_a_configured_503s_and_keeps_channel(
    app_session, authed_org_and_project, monkeypatch
):
    """The matching detach-side guarantee: a WhatsApp channel with a real
    `external_ref` must not disappear locally while Layer A's own Level C
    grant silently keeps capturing it — same fail-closed ordering as
    attach (app/api/channels.py's `detach_channel`)."""
    from app.capture.config import WhatsAppSettings
    from app.models import Channel

    org_id, project_id, _admin, admin_token = authed_org_and_project
    await set_org_context(app_session, org_id)
    channel = Channel(project_id=project_id, type="whatsapp", external_ref="120363297803566811@g.us")
    app_session.add(channel)
    await app_session.commit()
    channel_id = channel.id

    monkeypatch.setattr(
        "app.capture.adapters.whatsapp.get_whatsapp_settings",
        lambda: WhatsAppSettings(session_endpoint=None, api_token=None),
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        detach = await client.delete(
            f"/projects/{project_id}/channels/{channel_id}", headers=_headers(admin_token)
        )
        assert detach.status_code == 503, detach.text

        listing = await client.get(f"/projects/{project_id}/channels", headers=_headers(admin_token))
    assert len(listing.json()) == 1
    assert listing.json()[0]["id"] == str(channel_id)


@pytest.mark.asyncio
async def test_detach_whatsapp_channel_without_external_ref_skips_allowlist_call(
    app_session, authed_org_and_project, monkeypatch
):
    """A WhatsApp Channel row with no `external_ref` (never actually
    resolved to a real jid) has nothing to revoke — detach must not attempt
    an allowlist call at all, confirmed by pointing WhatsApp settings at
    something that would error if called."""
    from app.capture.config import WhatsAppSettings
    from app.models import Channel

    org_id, project_id, _admin, admin_token = authed_org_and_project
    await set_org_context(app_session, org_id)
    channel = Channel(project_id=project_id, type="whatsapp", external_ref=None)
    app_session.add(channel)
    await app_session.commit()
    channel_id = channel.id

    monkeypatch.setattr(
        "app.capture.adapters.whatsapp.get_whatsapp_settings",
        lambda: WhatsAppSettings(session_endpoint=None, api_token=None),
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        detach = await client.delete(
            f"/projects/{project_id}/channels/{channel_id}", headers=_headers(admin_token)
        )

    assert detach.status_code == 204


@pytest.mark.asyncio
async def test_channel_not_found_404s(authed_org_and_project):
    org_id, project_id, _admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/channels/{uuid.uuid4()}/reconnect", headers=_headers(admin_token)
        )
    assert response.status_code == 404


# --- Capture debug console: GET .../messages, POST .../capture/pull-now ----


@pytest.mark.asyncio
async def test_list_channel_messages_returns_real_captured_rows_in_order(authed_org_and_project):
    """Real capture, not fabricated rows: runs the same `ingest_channel_job`
    the arq worker itself calls (fixture-backed — `CUE_CAPTURE_BACKEND=
    fixture` is this suite's default, no live credentials needed), then
    confirms the new debug-console endpoint reads those real `Message` rows
    back, chronologically."""
    from app.capture.worker import ingest_channel_job
    from tests.test_capture_worker import _NoCommitmentsClient

    org_id, project_id, _admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            f"/projects/{project_id}/channels",
            headers=_headers(admin_token),
            json={"type": "whatsapp", "external_ref": "g1"},
        )
        channel_id = created.json()["id"]

        result = await ingest_channel_job(
            None, str(org_id), channel_id, client=_NoCommitmentsClient()
        )
        assert result["new_messages"] > 0

        response = await client.get(
            f"/projects/{project_id}/channels/{channel_id}/messages", headers=_headers(admin_token)
        )

    assert response.status_code == 200, response.text
    messages = response.json()
    assert len(messages) == result["new_messages"]
    assert all(m["channel_id"] == channel_id for m in messages)
    sent_at_values = [m["sent_at"] for m in messages]
    assert sent_at_values == sorted(sent_at_values)
    # A real fixture case's text/sender, not a placeholder.
    assert all(m["text"] for m in messages)
    assert all(m["sender_external_id"] for m in messages)
    # Blind Spots item 6: FR-NRM-03's own resolved-identity confidence
    # (app/capture/identity.py's resolve_identity), set on every real
    # captured Message since M8 but never exposed by this debug view until
    # now — every fixture sender here is brand-new, so full confidence,
    # not manually verified.
    assert all(m["identity_confidence"] == 1.0 for m in messages)
    assert all(m["identity_manually_verified"] is False for m in messages)


@pytest.mark.asyncio
async def test_list_channel_messages_empty_before_any_capture(authed_org_and_project):
    org_id, project_id, _admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            f"/projects/{project_id}/channels",
            headers=_headers(admin_token),
            json={"type": "whatsapp", "external_ref": "g1"},
        )
        channel_id = created.json()["id"]

        response = await client.get(
            f"/projects/{project_id}/channels/{channel_id}/messages", headers=_headers(admin_token)
        )

    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_pull_channel_now_enqueues_a_real_job_against_real_valkey(authed_org_and_project):
    """`enqueue_channel_ingestion`'s own per-channel dedup-by-job-id is
    already proven for real against real Valkey by test_capture_worker.py
    — this test's own job is proving the API wiring: a first pull queues,
    an immediate second pull for the same channel (nothing has consumed
    the first job yet, since no worker is running in this test process)
    correctly reports `queued: False`, not an error."""
    org_id, project_id, _admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            f"/projects/{project_id}/channels",
            headers=_headers(admin_token),
            json={"type": "whatsapp", "external_ref": "g1"},
        )
        channel_id = created.json()["id"]

        first = await client.post(
            f"/projects/{project_id}/channels/{channel_id}/capture/pull-now", headers=_headers(admin_token)
        )
        second = await client.post(
            f"/projects/{project_id}/channels/{channel_id}/capture/pull-now", headers=_headers(admin_token)
        )

    assert first.status_code == 200, first.text
    assert first.json() == {"queued": True}
    assert second.status_code == 200, second.text
    assert second.json() == {"queued": False}


@pytest.mark.asyncio
async def test_pull_channel_now_rejects_a_channel_from_a_different_project(
    authed_org_and_project, app_session, seeded_vertical_id
):
    """The real bug this endpoint's own review caught: `enqueue_channel_
    ingestion` takes a bare channel id with no project/org check of its
    own, so the endpoint must reject a channel id that doesn't belong to
    the calling project itself, before ever reaching the queue."""
    from app.models import Channel, Organisation, Project

    org_id, project_id, admin, admin_token = authed_org_and_project

    other_org_id = uuid.uuid4()
    await set_org_context(app_session, other_org_id)
    app_session.add(Organisation(id=other_org_id, name="Other Org"))
    await app_session.flush()
    other_project = Project(
        organisation_id=other_org_id,
        vertical_id=seeded_vertical_id,
        name="Other Project",
        timezone="Asia/Singapore",
    )
    app_session.add(other_project)
    await app_session.flush()
    other_channel = Channel(project_id=other_project.id, type="whatsapp", external_ref="g1", healthy=True)
    app_session.add(other_channel)
    await app_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/channels/{other_channel.id}/capture/pull-now",
            headers=_headers(admin_token),
        )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_designer_cannot_pull_channel_now(app_session, authed_org_and_project):
    org_id, project_id, admin, admin_token = authed_org_and_project
    _designer, designer_token = await _member(app_session, org_id, project_id, "designer", admin.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            f"/projects/{project_id}/channels",
            headers=_headers(admin_token),
            json={"type": "whatsapp", "external_ref": "g1"},
        )
        channel_id = created.json()["id"]

        response = await client.post(
            f"/projects/{project_id}/channels/{channel_id}/capture/pull-now",
            headers=_headers(designer_token),
        )
    assert response.status_code == 403


# --- Capture status: GET .../capture/status -------------------------------


@pytest.mark.asyncio
async def test_capture_status_not_found_before_any_pull(authed_org_and_project):
    org_id, project_id, _admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            f"/projects/{project_id}/channels",
            headers=_headers(admin_token),
            json={"type": "whatsapp", "external_ref": "g1"},
        )
        channel_id = created.json()["id"]

        response = await client.get(
            f"/projects/{project_id}/channels/{channel_id}/capture/status", headers=_headers(admin_token)
        )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "not_found"


@pytest.mark.asyncio
async def test_capture_status_reports_a_real_completed_pull(authed_org_and_project, monkeypatch):
    """The point of this test: `GET .../capture/status` reads arq's *own*
    job state (`Job.status()`/`result_info()`), which only exists once a
    real arq `Worker` actually executes the job — calling
    `ingest_channel_job` directly (as most of this file's other tests do)
    would never populate that state at all, and this endpoint would report
    `not_found` regardless of whether extraction genuinely ran. So this one
    test runs a real, burst-mode `arq.worker.Worker` against the real
    queued job, the same dispatch path production uses.

    `app.ledger.extractor.get_client` monkeypatched to the real `FakeClient`
    (app/llm/client.py, CI's own extraction stand-in) — necessary because a
    real arq dispatch can't be given the keyword-only `client=` test seam
    `ingest_channel_job` documents for direct calls (arq only ever passes
    the positional args `enqueue_channel_ingestion` gave it), so without
    this the job would fall through to a real, live-Ollama extraction call
    the rest of this suite deliberately never depends on."""
    from arq.connections import RedisSettings, create_pool
    from arq.worker import Worker

    from app.capture.worker import ingest_channel_job
    from app.foresight.config import get_arq_settings
    from app.llm.client import FakeClient

    monkeypatch.setattr("app.ledger.extractor.get_client", lambda role: FakeClient())

    org_id, project_id, _admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            f"/projects/{project_id}/channels",
            headers=_headers(admin_token),
            json={"type": "whatsapp", "external_ref": "g1"},
        )
        channel_id = created.json()["id"]

        pulled = await client.post(
            f"/projects/{project_id}/channels/{channel_id}/capture/pull-now", headers=_headers(admin_token)
        )
        assert pulled.json()["queued"] is True

        arq_settings = get_arq_settings()
        redis = await create_pool(RedisSettings(host=arq_settings.redis_host, port=arq_settings.redis_port))
        worker = Worker(functions=[ingest_channel_job], redis_pool=redis, burst=True)
        try:
            await worker.async_run()
        finally:
            await worker.close()

        response = await client.get(
            f"/projects/{project_id}/channels/{channel_id}/capture/status", headers=_headers(admin_token)
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "complete"
    assert body["success"] is True
    assert body["received"] == 6
    assert body["new_messages"] == 6
    assert body["skipped"] is None


@pytest.mark.asyncio
async def test_capture_status_reports_a_real_failure(authed_org_and_project, monkeypatch):
    """The matching failure-path proof: a job that genuinely raises (a
    broken extraction client, standing in for any real failure mode) must
    surface as `status="complete", success=False`, with a real error
    message — not silently swallowed, and not confused with the
    nothing-new-to-capture success case."""
    from arq.connections import RedisSettings, create_pool
    from arq.worker import Worker

    from app.capture.worker import ingest_channel_job
    from app.foresight.config import get_arq_settings

    class _BrokenClient:
        async def complete(self, prompt: str, schema: dict):
            raise RuntimeError("simulated extraction failure")

    monkeypatch.setattr("app.ledger.extractor.get_client", lambda role: _BrokenClient())

    org_id, project_id, _admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            f"/projects/{project_id}/channels",
            headers=_headers(admin_token),
            json={"type": "whatsapp", "external_ref": "g1"},
        )
        channel_id = created.json()["id"]

        await client.post(
            f"/projects/{project_id}/channels/{channel_id}/capture/pull-now", headers=_headers(admin_token)
        )

        arq_settings = get_arq_settings()
        redis = await create_pool(RedisSettings(host=arq_settings.redis_host, port=arq_settings.redis_port))
        worker = Worker(functions=[ingest_channel_job], redis_pool=redis, burst=True, max_tries=1)
        try:
            await worker.async_run()
        finally:
            await worker.close()

        response = await client.get(
            f"/projects/{project_id}/channels/{channel_id}/capture/status", headers=_headers(admin_token)
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "complete"
    assert body["success"] is False
    assert "simulated extraction failure" in body["error"]


@pytest.mark.asyncio
async def test_capture_status_rejects_a_channel_from_a_different_project(
    authed_org_and_project, app_session, seeded_vertical_id
):
    from app.models import Channel, Organisation, Project

    org_id, project_id, admin, admin_token = authed_org_and_project

    other_org_id = uuid.uuid4()
    await set_org_context(app_session, other_org_id)
    app_session.add(Organisation(id=other_org_id, name="Other Org"))
    await app_session.flush()
    other_project = Project(
        organisation_id=other_org_id,
        vertical_id=seeded_vertical_id,
        name="Other Project",
        timezone="Asia/Singapore",
    )
    app_session.add(other_project)
    await app_session.flush()
    other_channel = Channel(project_id=other_project.id, type="whatsapp", external_ref="g1", healthy=True)
    app_session.add(other_channel)
    await app_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/projects/{project_id}/channels/{other_channel.id}/capture/status",
            headers=_headers(admin_token),
        )

    assert response.status_code == 404
