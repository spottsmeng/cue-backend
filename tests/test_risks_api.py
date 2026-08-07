"""REST endpoints for /projects/{id}/risks — list · read · acknowledge ·
resolve. RLS and role-gating tested as two independent properties, same
split tests/test_twin_api.py's own module docstring establishes.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.foresight.models import Risk
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
        organisation_id=org_id, issuer=get_identity_settings().local_issuer,
        external_subject=subject, email=f"{subject}@example.test",
    )
    app_session.add(user)
    await app_session.flush()
    app_session.add(Membership(user_id=user.id, project_id=project_id, role=role, granted_by=granted_by))
    await app_session.commit()
    return user, mint_token(org_id, subject=subject, email=user.email)


async def _make_risk(app_session, project_id, **overrides) -> Risk:
    defaults = dict(
        project_id=project_id, source="silence", finding_key=f"silence:{uuid.uuid4()}", severity="high",
        status="open", downstream_consequence="test fixture risk",
    )
    defaults.update(overrides)
    risk = Risk(**defaults)
    app_session.add(risk)
    await app_session.commit()
    return risk


@pytest.mark.asyncio
async def test_list_and_read_risk(app_session, authed_org_and_project):
    org_id, project_id, _user, token = authed_org_and_project
    await set_org_context(app_session, org_id)
    risk = await _make_risk(app_session, project_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        listing = await client.get(f"/projects/{project_id}/risks", headers=_headers(token))
        read = await client.get(f"/projects/{project_id}/risks/{risk.id}", headers=_headers(token))

    assert listing.status_code == 200
    assert str(risk.id) in [r["id"] for r in listing.json()]
    assert read.status_code == 200
    assert read.json()["downstream_consequence"] == "test fixture risk"


@pytest.mark.asyncio
async def test_acknowledge_risk_stamps_actor_and_time(app_session, authed_org_and_project):
    org_id, project_id, user, token = authed_org_and_project
    await set_org_context(app_session, org_id)
    risk = await _make_risk(app_session, project_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/risks/{risk.id}/acknowledge", headers=_headers(token)
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "acknowledged"
    assert body["acknowledged_by"] == str(user.id)
    assert body["acknowledged_at"] is not None


@pytest.mark.asyncio
async def test_cannot_acknowledge_an_already_resolved_risk(app_session, authed_org_and_project):
    org_id, project_id, _user, token = authed_org_and_project
    await set_org_context(app_session, org_id)
    risk = await _make_risk(app_session, project_id, status="resolved")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/risks/{risk.id}/acknowledge", headers=_headers(token)
        )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_read_only_member_can_read_but_not_acknowledge(app_session, authed_org_and_project):
    org_id, project_id, admin, _admin_token = authed_org_and_project
    await set_org_context(app_session, org_id)
    risk = await _make_risk(app_session, project_id)
    _viewer, viewer_token = await _member(app_session, org_id, project_id, "read_only", admin.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        read = await client.get(f"/projects/{project_id}/risks", headers=_headers(viewer_token))
        ack = await client.post(
            f"/projects/{project_id}/risks/{risk.id}/acknowledge", headers=_headers(viewer_token)
        )

    assert read.status_code == 200
    assert ack.status_code == 403


@pytest.mark.asyncio
async def test_risks_are_isolated_via_project_join_rls(app_session, authed_org_and_project):
    org_id, project_id, _user, _token = authed_org_and_project
    await set_org_context(app_session, org_id)
    risk = await _make_risk(app_session, project_id)

    await set_org_context(app_session, uuid.uuid4())  # a different, unrelated org
    result = await app_session.execute(select(Risk).where(Risk.id == risk.id))
    assert result.scalar_one_or_none() is None
