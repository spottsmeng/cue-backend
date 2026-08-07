"""REST endpoints for /admin/retention (PRD §6.14 FR-ADM-08, §10.3):
retention window configuration, per org x vertical x region — config only,
no deletion automation (out of scope this session). Gated by
require_org_administrator, same as /admin/consent.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from main import app


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_create_and_list_retention_policy(authed_org_and_project):
    org_id, project_id, _admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/admin/retention",
            headers=_headers(admin_token),
            json={"region": "SG", "retention_days": 2555},
        )
        listing = await client.get("/admin/retention", headers=_headers(admin_token))

    assert created.status_code == 201, created.text
    body = created.json()
    assert body["region"] == "SG"
    assert body["retention_days"] == 2555
    assert body["organisation_id"] == str(org_id)
    assert len(listing.json()) == 1


@pytest.mark.asyncio
async def test_create_retention_policy_with_vertical(authed_org_and_project):
    org_id, project_id, _admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project = await client.get(f"/projects/{project_id}", headers=_headers(admin_token))
        vertical_id = project.json()["vertical_id"]

        response = await client.post(
            "/admin/retention",
            headers=_headers(admin_token),
            json={"vertical_id": vertical_id, "retention_days": 90},
        )
    assert response.status_code == 201, response.text
    assert response.json()["vertical_id"] == vertical_id


@pytest.mark.asyncio
async def test_create_retention_policy_unknown_vertical_is_rejected(authed_org_and_project):
    org_id, project_id, _admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/admin/retention",
            headers=_headers(admin_token),
            json={"vertical_id": str(uuid.uuid4()), "retention_days": 90},
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_non_positive_retention_days_is_rejected(authed_org_and_project):
    org_id, project_id, _admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/admin/retention", headers=_headers(admin_token), json={"retention_days": 0}
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_update_retention_policy(authed_org_and_project):
    org_id, project_id, _admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/admin/retention", headers=_headers(admin_token), json={"retention_days": 365}
        )
        policy_id = created.json()["id"]

        updated = await client.patch(
            f"/admin/retention/{policy_id}",
            headers=_headers(admin_token),
            json={"retention_days": 2555, "region": "CN"},
        )

    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["retention_days"] == 2555
    assert body["region"] == "CN"


@pytest.mark.asyncio
async def test_delete_retention_policy(authed_org_and_project):
    org_id, project_id, _admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/admin/retention", headers=_headers(admin_token), json={"retention_days": 365}
        )
        policy_id = created.json()["id"]

        deleted = await client.delete(f"/admin/retention/{policy_id}", headers=_headers(admin_token))
        listing = await client.get("/admin/retention", headers=_headers(admin_token))

    assert deleted.status_code == 204
    assert listing.json() == []


@pytest.mark.asyncio
async def test_retention_policy_not_found_404s(authed_org_and_project):
    org_id, project_id, _admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/admin/retention/{uuid.uuid4()}", headers=_headers(admin_token)
        )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_non_administrator_cannot_manage_retention(app_session, authed_org_and_project):
    from app.identity.config import get_identity_settings
    from app.identity.models import Membership, User
    from tests.conftest import mint_token, set_org_context

    org_id, project_id, admin, _admin_token = authed_org_and_project
    await set_org_context(app_session, org_id)
    subject = f"finance-{uuid.uuid4()}"
    finance_user = User(
        organisation_id=org_id,
        issuer=get_identity_settings().local_issuer,
        external_subject=subject,
        email=f"{subject}@example.test",
    )
    app_session.add(finance_user)
    await app_session.flush()
    app_session.add(
        Membership(user_id=finance_user.id, project_id=project_id, role="finance", granted_by=admin.id)
    )
    await app_session.commit()
    finance_token = mint_token(org_id, subject=subject, email=finance_user.email)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/admin/retention", headers=_headers(finance_token))
    assert response.status_code == 403
