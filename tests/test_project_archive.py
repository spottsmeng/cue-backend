"""POST /projects/{id}/archive (§11.2, FR-DOC-06). ADMIN_ROLES-gated, sets
Project.archived_at, resolves and records the applicable RetentionPolicy on
document_audit_log — no prior session had ever exercised this column or
this action (backend/PROGRESS.md's own note on the gap this closes).
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.documents.models import DocumentAuditLog
from app.identity.config import get_identity_settings
from app.identity.models import Membership, User
from app.models import RetentionPolicy
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
async def test_administrator_can_archive_project(authed_org_and_project):
    org_id, project_id, admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/archive", headers=_headers(admin_token)
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["project"]["archived_at"] is not None
    assert body["retention_policy_id"] is None  # no RetentionPolicy configured for this org


@pytest.mark.asyncio
async def test_non_admin_role_cannot_archive_project(app_session, authed_org_and_project):
    org_id, project_id, admin, _admin_token = authed_org_and_project
    _designer, designer_token = await _member(app_session, org_id, project_id, "designer", admin.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/archive", headers=_headers(designer_token)
        )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_archiving_twice_is_409(authed_org_and_project):
    org_id, project_id, admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(f"/projects/{project_id}/archive", headers=_headers(admin_token))
        second = await client.post(f"/projects/{project_id}/archive", headers=_headers(admin_token))

    assert first.status_code == 200
    assert second.status_code == 409


@pytest.mark.asyncio
async def test_archive_resolves_configured_retention_policy(app_session, authed_org_and_project):
    """FR-DOC-06: 'apply the org/project's RetentionPolicy' — resolution
    only (no deletion scheduler), recorded on document_audit_log."""
    org_id, project_id, admin, admin_token = authed_org_and_project

    await set_org_context(app_session, org_id)
    app_session.add(RetentionPolicy(organisation_id=org_id, vertical_id=None, retention_days=365))
    await app_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/archive", headers=_headers(admin_token)
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["retention_days"] == 365
    assert body["retention_policy_id"] is not None

    await set_org_context(app_session, org_id)
    audit_rows = (
        await app_session.execute(
            select(DocumentAuditLog).where(
                DocumentAuditLog.project_id == project_id,
                DocumentAuditLog.action == "project_archived",
            )
        )
    ).scalars().all()
    assert len(audit_rows) == 1
    assert audit_rows[0].detail["retention_days"] == 365
