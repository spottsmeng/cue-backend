"""GET/PATCH /users/me (app/api/users.py) — F9's per-user preference
surface (NFR-ACC-03's high-contrast mode). Not project-scoped, so
`org_and_project` (an org, no membership needed) is enough."""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from main import app
from tests.conftest import auth_headers, mint_token


@pytest.mark.asyncio
async def test_get_me_defaults_high_contrast_false(app_session, org_and_project):
    org_id, _project_id = org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/users/me", headers=auth_headers(org_id))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["high_contrast"] is False
    assert body["email"]


@pytest.mark.asyncio
async def test_patch_me_flips_high_contrast_and_persists(app_session, org_and_project):
    org_id, _project_id = org_and_project
    token = mint_token(org_id)
    headers = {"Authorization": f"Bearer {token}"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        patch_response = await client.patch(
            "/users/me", json={"high_contrast": True}, headers=headers
        )
        assert patch_response.status_code == 200, patch_response.text
        assert patch_response.json()["high_contrast"] is True

        get_response = await client.get("/users/me", headers=headers)
        assert get_response.status_code == 200, get_response.text
        assert get_response.json()["high_contrast"] is True


@pytest.mark.asyncio
async def test_patch_me_does_not_leak_across_users(app_session, org_and_project):
    org_id, _project_id = org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first_response = await client.patch(
            "/users/me", json={"high_contrast": True}, headers=auth_headers(org_id)
        )
        assert first_response.status_code == 200, first_response.text

        second_response = await client.get("/users/me", headers=auth_headers(org_id))

    assert second_response.status_code == 200, second_response.text
    assert second_response.json()["high_contrast"] is False
    assert second_response.json()["id"] != first_response.json()["id"]


@pytest.mark.asyncio
async def test_me_requires_authentication():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/users/me")

    assert response.status_code in (401, 403)
