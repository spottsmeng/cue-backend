"""Role-based access control (PRD §6.14, FR-ADM-01) through the real ASGI
app — every request authenticated with a real bearer token
(tests/conftest.py's mint_token), exercising app/api/deps.py's
require_project_role and app/identity/service.py's effective_roles for real,
not by constructing role sets in Python and asserting on them in isolation.
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
    """A real `users` row plus a `memberships` row granting `role` on
    `project_id`, and a bearer token authenticating as them."""
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


def _commitment_body(vendor_id, internal_id):
    return {
        "party_id": str(vendor_id),
        "counterparty_id": str(internal_id),
        "act_type": "commit",
        "deliverable_en": "LED screen install",
    }


@pytest.mark.asyncio
async def test_read_only_member_cannot_create_commitment(app_session, authed_org_and_project, parties):
    org_id, project_id, admin, _admin_token = authed_org_and_project
    vendor, internal = parties
    _viewer, viewer_token = await _member(app_session, org_id, project_id, "read_only", admin.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/commitments",
            headers=_headers(viewer_token),
            json=_commitment_body(vendor.id, internal.id),
        )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_read_only_member_can_read_commitments(app_session, authed_org_and_project, parties):
    """The role that exists specifically to grant visibility without write
    access must still grant that visibility (FR-ADM-02 is about *which*
    projects are visible, not gating read access by role)."""
    org_id, project_id, admin, admin_token = authed_org_and_project
    vendor, internal = parties
    _viewer, viewer_token = await _member(app_session, org_id, project_id, "read_only", admin.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            f"/projects/{project_id}/commitments",
            headers=_headers(admin_token),
            json=_commitment_body(vendor.id, internal.id),
        )
        commitment_id = created.json()["id"]

        response = await client.get(
            f"/projects/{project_id}/commitments/{commitment_id}", headers=_headers(viewer_token)
        )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_producer_member_can_create_commitment(app_session, authed_org_and_project, parties):
    org_id, project_id, admin, _admin_token = authed_org_and_project
    vendor, internal = parties
    _producer, producer_token = await _member(app_session, org_id, project_id, "producer", admin.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/commitments",
            headers=_headers(producer_token),
            json=_commitment_body(vendor.id, internal.id),
        )
    assert response.status_code == 201, response.text


@pytest.mark.asyncio
async def test_delegate_role_without_active_delegation_grants_no_write_access(
    app_session, authed_org_and_project, parties
):
    """"Delegate" is a distinct FR-ADM-01 role value, not a synonym for
    "has some access" — holding it alone (no active app/identity Delegation
    row pointing at this user) must behave like read_only for writes."""
    org_id, project_id, admin, _admin_token = authed_org_and_project
    vendor, internal = parties
    _stand_in, stand_in_token = await _member(app_session, org_id, project_id, "delegate", admin.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/commitments",
            headers=_headers(stand_in_token),
            json=_commitment_body(vendor.id, internal.id),
        )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_non_member_gets_404_not_403(app_session, authed_org_and_project):
    """FR-ADM-02: a project a user isn't assigned to must look exactly like a
    project that doesn't exist — never a 403 that would confirm it does."""
    org_id, project_id, _admin, _admin_token = authed_org_and_project
    outsider_token = mint_token(org_id)  # same org, fresh identity, never added as a member

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/projects/{project_id}", headers=_headers(outsider_token))
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_only_admin_roles_can_add_members(app_session, authed_org_and_project):
    org_id, project_id, admin, admin_token = authed_org_and_project
    _designer, designer_token = await _member(app_session, org_id, project_id, "designer", admin.id)

    target_subject = f"target-{uuid.uuid4()}"
    await set_org_context(app_session, org_id)
    target = User(
        organisation_id=org_id,
        issuer=get_identity_settings().local_issuer,
        external_subject=target_subject,
        email=f"{target_subject}@example.test",
    )
    app_session.add(target)
    await app_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.post(
            f"/projects/{project_id}/members",
            headers=_headers(designer_token),
            json={"email": target.email, "role": "finance"},
        )
        assert denied.status_code == 403

        allowed = await client.post(
            f"/projects/{project_id}/members",
            headers=_headers(admin_token),
            json={"email": target.email, "role": "finance"},
        )
        assert allowed.status_code == 201, allowed.text
        assert allowed.json()["role"] == "finance"


@pytest.mark.asyncio
async def test_add_member_unknown_email_is_rejected(authed_org_and_project):
    """No SCIM pre-provisioning this session — a member can only be assigned
    once they've authenticated at least once."""
    org_id, project_id, admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/members",
            headers=_headers(admin_token),
            json={"email": "nobody@example.test", "role": "designer"},
        )
    assert response.status_code == 422
