"""FR-ADM-03 (time-boxed delegation) and FR-ADM-04 (full audit of delegation
grants) — through the real ASGI app for the grant/revoke lifecycle, and
directly against the database for the DB-enforced audit-trail properties
(same "verified in code, not trusted" posture test_audit_log.py gives the
commitment audit trail).
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.identity.config import get_identity_settings
from app.identity.models import Delegation, Membership, User
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


async def _bare_user(app_session, org_id):
    """A real `users` row with no membership on any project at all — the
    "someone who wasn't previously on this project" case delegation exists
    for."""
    await set_org_context(app_session, org_id)
    subject = f"outsider-{uuid.uuid4()}"
    user = User(
        organisation_id=org_id,
        issuer=get_identity_settings().local_issuer,
        external_subject=subject,
        email=f"{subject}@example.test",
    )
    app_session.add(user)
    await app_session.commit()
    return user, mint_token(org_id, subject=subject, email=user.email)


def _commitment_body(vendor_id, internal_id):
    return {
        "party_id": str(vendor_id),
        "counterparty_id": str(internal_id),
        "act_type": "commit",
        "deliverable_en": "LED screen install",
    }


def _future(hours=1) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


@pytest.mark.asyncio
async def test_administrator_can_delegate_any_role_to_a_non_member(
    app_session, authed_org_and_project, parties
):
    org_id, project_id, admin, admin_token = authed_org_and_project
    vendor, internal = parties
    outsider, outsider_token = await _bare_user(app_session, org_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Before delegation: an outsider with no membership can't even see
        # the project (FR-ADM-02), let alone write to it.
        pre = await client.get(f"/projects/{project_id}", headers=_headers(outsider_token))
        assert pre.status_code == 404

        grant = await client.post(
            f"/projects/{project_id}/delegations",
            headers=_headers(admin_token),
            json={"delegate_email": outsider.email, "role": "finance", "expires_at": _future()},
        )
        assert grant.status_code == 201, grant.text
        body = grant.json()
        assert body["delegator_id"] == str(admin.id)
        assert body["delegate_id"] == str(outsider.id)
        assert body["role"] == "finance"
        assert body["granted_by"] == str(admin.id)
        assert body["revoked_at"] is None

        # After delegation: the same outsider, same token, now a "delegate"
        # with finance-level write access — no membership row was ever
        # created for them.
        post = await client.get(f"/projects/{project_id}", headers=_headers(outsider_token))
        assert post.status_code == 200

        listing = await client.get("/projects", headers=_headers(outsider_token))
        assert str(project_id) in [p["id"] for p in listing.json()]

        create = await client.post(
            f"/projects/{project_id}/commitments",
            headers=_headers(outsider_token),
            json=_commitment_body(vendor.id, internal.id),
        )
        assert create.status_code == 201, create.text


@pytest.mark.asyncio
async def test_member_can_only_delegate_a_role_they_hold(app_session, authed_org_and_project):
    org_id, project_id, admin, _admin_token = authed_org_and_project
    designer, designer_token = await _member(app_session, org_id, project_id, "designer", admin.id)
    outsider, _outsider_token = await _bare_user(app_session, org_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.post(
            f"/projects/{project_id}/delegations",
            headers=_headers(designer_token),
            json={"delegate_email": outsider.email, "role": "finance", "expires_at": _future()},
        )
        assert denied.status_code == 403

        allowed = await client.post(
            f"/projects/{project_id}/delegations",
            headers=_headers(designer_token),
            json={"delegate_email": outsider.email, "role": "designer", "expires_at": _future()},
        )
        assert allowed.status_code == 201, allowed.text


@pytest.mark.asyncio
async def test_cannot_delegate_to_self(authed_org_and_project):
    org_id, project_id, admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/delegations",
            headers=_headers(admin_token),
            json={"delegate_email": admin.email, "role": "administrator", "expires_at": _future()},
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_expires_at_must_be_in_the_future(app_session, authed_org_and_project):
    org_id, project_id, admin, admin_token = authed_org_and_project
    outsider, _token = await _bare_user(app_session, org_id)
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/delegations",
            headers=_headers(admin_token),
            json={"delegate_email": outsider.email, "role": "finance", "expires_at": past},
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_expired_delegation_stops_granting_access(app_session, authed_org_and_project, parties):
    """Checked at request time, not a scheduled sweep (app/identity/models.py's
    Delegation docstring) — a delegation that has simply run past its
    expires_at must stop working on the very next request, with no cleanup
    job required."""
    org_id, project_id, admin, _admin_token = authed_org_and_project
    vendor, internal = parties
    outsider, outsider_token = await _bare_user(app_session, org_id)

    await set_org_context(app_session, org_id)
    now = datetime.now(timezone.utc)
    app_session.add(
        Delegation(
            project_id=project_id,
            delegator_id=admin.id,
            delegate_id=outsider.id,
            role="producer",
            granted_at=now - timedelta(hours=2),
            granted_by=admin.id,
            expires_at=now - timedelta(hours=1),
        )
    )
    await app_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/commitments",
            headers=_headers(outsider_token),
            json=_commitment_body(vendor.id, internal.id),
        )
    assert response.status_code == 404  # expired -> no membership at all, same as never delegated


@pytest.mark.asyncio
async def test_revoked_delegation_stops_granting_access(app_session, authed_org_and_project, parties):
    org_id, project_id, admin, admin_token = authed_org_and_project
    vendor, internal = parties
    outsider, outsider_token = await _bare_user(app_session, org_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        grant = await client.post(
            f"/projects/{project_id}/delegations",
            headers=_headers(admin_token),
            json={"delegate_email": outsider.email, "role": "producer", "expires_at": _future()},
        )
        delegation_id = grant.json()["id"]

        pre = await client.post(
            f"/projects/{project_id}/commitments",
            headers=_headers(outsider_token),
            json=_commitment_body(vendor.id, internal.id),
        )
        assert pre.status_code == 201, pre.text

        revoke = await client.post(
            f"/projects/{project_id}/delegations/{delegation_id}/revoke",
            headers=_headers(admin_token),
        )
        assert revoke.status_code == 200, revoke.text
        assert revoke.json()["revoked_at"] is not None
        assert revoke.json()["revoked_by"] == str(admin.id)

        post = await client.post(
            f"/projects/{project_id}/commitments",
            headers=_headers(outsider_token),
            json=_commitment_body(vendor.id, internal.id),
        )
        assert post.status_code == 404

        again = await client.post(
            f"/projects/{project_id}/delegations/{delegation_id}/revoke",
            headers=_headers(admin_token),
        )
        assert again.status_code == 409


@pytest.mark.asyncio
async def test_only_delegator_or_admin_can_revoke(app_session, authed_org_and_project):
    org_id, project_id, admin, admin_token = authed_org_and_project
    designer, designer_token = await _member(app_session, org_id, project_id, "designer", admin.id)
    outsider, _token = await _bare_user(app_session, org_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        grant = await client.post(
            f"/projects/{project_id}/delegations",
            headers=_headers(admin_token),
            json={"delegate_email": outsider.email, "role": "producer", "expires_at": _future()},
        )
        delegation_id = grant.json()["id"]

        # designer is a real, unrelated member — neither the delegator nor
        # an administrator on this project.
        denied = await client.post(
            f"/projects/{project_id}/delegations/{delegation_id}/revoke",
            headers=_headers(designer_token),
        )
        assert denied.status_code == 403

        allowed = await client.post(
            f"/projects/{project_id}/delegations/{delegation_id}/revoke",
            headers=_headers(admin_token),
        )
        assert allowed.status_code == 200


@pytest.mark.asyncio
async def test_delegation_audit_trail_is_append_only_except_revocation(
    app_session, authed_org_and_project
):
    """FR-ADM-04: the DB trigger (migration 1b4a45ebc039), not just the
    application choosing not to issue other mutations — same posture
    test_audit_log.py proves for the commitment audit trail."""
    org_id, project_id, admin, _admin_token = authed_org_and_project
    admin_id = admin.id  # captured before a later rollback expires the instance
    outsider, _token = await _bare_user(app_session, org_id)
    await set_org_context(app_session, org_id)

    delegation = Delegation(
        project_id=project_id,
        delegator_id=admin_id,
        delegate_id=outsider.id,
        role="producer",
        granted_by=admin_id,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    app_session.add(delegation)
    await app_session.commit()
    delegation_id = delegation.id  # captured before rollback expires the instance

    with pytest.raises(DBAPIError, match="append-only"):
        await app_session.execute(
            text("UPDATE delegations SET role = 'finance' WHERE id = :id"), {"id": delegation_id}
        )
    await app_session.rollback()

    with pytest.raises(DBAPIError, match="append-only"):
        await app_session.execute(text("DELETE FROM delegations WHERE id = :id"), {"id": delegation_id})
    await app_session.rollback()

    # The one legitimate mutation: revocation, via revoked_at/revoked_by only.
    await app_session.execute(
        text("UPDATE delegations SET revoked_at = now(), revoked_by = :by WHERE id = :id"),
        {"by": admin_id, "id": delegation_id},
    )
    await app_session.commit()

    refreshed = (
        await app_session.execute(select(Delegation).where(Delegation.id == delegation_id))
    ).scalar_one()
    assert refreshed.revoked_at is not None
    assert refreshed.revoked_by == admin_id
