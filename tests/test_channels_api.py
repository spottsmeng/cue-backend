"""REST endpoints for /projects/{id}/channels (PRD §11.2, FR-ADM-06/09):
attach, detach, health, reconnect — activating app/models/project.py's
Channel model, previously untouched by any endpoint.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

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
    org_id, project_id, _admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/channels",
            headers=_headers(admin_token),
            json={"type": "whatsapp", "external_ref": "group-123"},
        )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["type"] == "whatsapp"
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
async def test_channel_not_found_404s(authed_org_and_project):
    org_id, project_id, _admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/channels/{uuid.uuid4()}/reconnect", headers=_headers(admin_token)
        )
    assert response.status_code == 404
