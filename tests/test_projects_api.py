"""REST endpoints for /projects (PRD §11.2), through the real ASGI app —
same httpx.AsyncClient + ASGITransport pattern as test_health.py, exercising
the real get_current_user/RBAC dependency chain (app/api/deps.py) via a
bearer token (tests/conftest.py's mint_token/auth_headers), not a header a
caller can just set by hand.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from main import app
from tests.conftest import auth_headers, set_org_context


async def _make_organisation(app_session) -> uuid.UUID:
    """Organisations aren't created through this API (out of scope — SCIM/
    tenant provisioning is an external, ops-level step per PRD §6.14's
    explicit out-of-scope note), so test setup still writes the row directly,
    same as before real auth existed."""
    org_id = uuid.uuid4()
    await set_org_context(app_session, org_id)
    await app_session.execute(
        text("INSERT INTO organisations (id, name, created_at, updated_at) VALUES (:id, 'API Test Org', now(), now())"),
        {"id": org_id},
    )
    await app_session.commit()
    return org_id


@pytest.mark.asyncio
async def test_create_project_requires_authentication():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/projects", json={"name": "No Auth"})
    assert response.status_code in (401, 403)


@pytest.mark.asyncio
async def test_create_project_rejects_invalid_token():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/projects",
            headers={"Authorization": "Bearer not-a-real-token"},
            json={"name": "Bad Token"},
        )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_create_and_read_project(app_session, seeded_vertical_id):
    org_id = await _make_organisation(app_session)
    headers = auth_headers(org_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post(
            "/projects",
            headers=headers,
            json={"name": "Meridian Activation", "client_name": "Acme Corp"},
        )
        assert create_resp.status_code == 201, create_resp.text
        body = create_resp.json()
        assert body["name"] == "Meridian Activation"
        assert body["organisation_id"] == str(org_id)
        assert body["vertical_id"] == str(seeded_vertical_id)
        project_id = body["id"]

        # Same token/identity: the creator was auto-granted "administrator"
        # membership (FR-ADM-06), so they can read their own new project back.
        read_resp = await client.get(f"/projects/{project_id}", headers=headers)
        assert read_resp.status_code == 200
        assert read_resp.json()["id"] == project_id


@pytest.mark.asyncio
async def test_create_project_rejects_unknown_vertical_code(app_session):
    org_id = await _make_organisation(app_session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/projects",
            headers=auth_headers(org_id),
            json={"name": "Bad Vertical", "vertical_code": "not-a-real-vertical"},
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_list_projects_is_scoped_to_org_by_rls_and_membership(app_session, seeded_vertical_id):
    """Both properties at once here (each also gets its own dedicated test in
    test_membership_scoping.py): org_a's user never sees org_b's project
    (RLS — different tenant entirely) — a stronger isolation than plain
    membership scoping would give on its own, since RLS denies it at the
    database layer regardless of membership rows."""
    org_a = await _make_organisation(app_session)
    org_b = await _make_organisation(app_session)
    headers_a = auth_headers(org_a)
    headers_b = auth_headers(org_b)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/projects", headers=headers_a, json={"name": "Org A Project"})
        await client.post("/projects", headers=headers_b, json={"name": "Org B Project"})

        resp_a = await client.get("/projects", headers=headers_a)
        names_a = {p["name"] for p in resp_a.json()}
        assert "Org A Project" in names_a
        assert "Org B Project" not in names_a


@pytest.mark.asyncio
async def test_read_project_not_found(app_session):
    org_id = await _make_organisation(app_session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/projects/{uuid.uuid4()}", headers=auth_headers(org_id))
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_read_project_hides_projects_the_user_is_not_a_member_of(app_session):
    """FR-ADM-02: distinct from org isolation — same org, same RLS pass, but
    a second user who was never assigned to this project must not see it
    either. Regression guard for accidentally checking only RLS and treating
    that as sufficient (it isn't — see CLAUDE.md-style reasoning in
    app/api/deps.py's require_project_role)."""
    org_id = await _make_organisation(app_session)
    creator_headers = auth_headers(org_id)
    outsider_headers = auth_headers(org_id)  # same org, different (fresh) identity

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post(
            "/projects", headers=creator_headers, json={"name": "Members Only"}
        )
        project_id = create_resp.json()["id"]

        response = await client.get(f"/projects/{project_id}", headers=outsider_headers)
    assert response.status_code == 404
