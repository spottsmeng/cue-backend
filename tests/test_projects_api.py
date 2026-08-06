"""REST endpoints for /projects (PRD §11.2), through the real ASGI app —
same httpx.AsyncClient + ASGITransport pattern as test_health.py, so this
exercises the actual per-request get_org_id/get_session dependency chain,
not a shortcut around it.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from main import app
from tests.conftest import set_org_context


async def _make_organisation(app_session) -> uuid.UUID:
    org_id = uuid.uuid4()
    await set_org_context(app_session, org_id)
    await app_session.execute(
        text("INSERT INTO organisations (id, name, created_at, updated_at) VALUES (:id, 'API Test Org', now(), now())"),
        {"id": org_id},
    )
    await app_session.commit()
    return org_id


@pytest.mark.asyncio
async def test_create_project_requires_org_header():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/projects", json={"name": "No Org Header"})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_create_and_read_project(app_session, seeded_vertical_id):
    org_id = await _make_organisation(app_session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post(
            "/projects",
            headers={"X-Org-Id": str(org_id)},
            json={"name": "Meridian Activation", "client_name": "Acme Corp"},
        )
        assert create_resp.status_code == 201, create_resp.text
        body = create_resp.json()
        assert body["name"] == "Meridian Activation"
        assert body["organisation_id"] == str(org_id)
        assert body["vertical_id"] == str(seeded_vertical_id)
        project_id = body["id"]

        read_resp = await client.get(f"/projects/{project_id}", headers={"X-Org-Id": str(org_id)})
        assert read_resp.status_code == 200
        assert read_resp.json()["id"] == project_id


@pytest.mark.asyncio
async def test_create_project_rejects_unknown_vertical_code(app_session):
    org_id = await _make_organisation(app_session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/projects",
            headers={"X-Org-Id": str(org_id)},
            json={"name": "Bad Vertical", "vertical_code": "not-a-real-vertical"},
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_list_projects_is_scoped_to_org_by_rls(app_session, seeded_vertical_id):
    org_a = await _make_organisation(app_session)
    org_b = await _make_organisation(app_session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/projects", headers={"X-Org-Id": str(org_a)}, json={"name": "Org A Project"}
        )
        await client.post(
            "/projects", headers={"X-Org-Id": str(org_b)}, json={"name": "Org B Project"}
        )

        resp_a = await client.get("/projects", headers={"X-Org-Id": str(org_a)})
        names_a = {p["name"] for p in resp_a.json()}
        assert "Org A Project" in names_a
        assert "Org B Project" not in names_a


@pytest.mark.asyncio
async def test_read_project_not_found(app_session):
    org_id = await _make_organisation(app_session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/projects/{uuid.uuid4()}", headers={"X-Org-Id": str(org_id)}
        )
    assert response.status_code == 404
