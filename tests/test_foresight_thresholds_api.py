"""REST endpoints for /admin/foresight-thresholds (FR-FOR-07) and
/projects/{id}/quiet-hours (FR-NTF-04). Thresholds mirror
tests/test_retention_api.py's org-admin-gated shape exactly (ForesightThreshold
extends RetentionPolicy's own config-table pattern); quiet hours are
write-role-gated, ordinary project configuration.
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
        organisation_id=org_id, issuer=get_identity_settings().local_issuer,
        external_subject=subject, email=f"{subject}@example.test",
    )
    app_session.add(user)
    await app_session.flush()
    app_session.add(Membership(user_id=user.id, project_id=project_id, role=role, granted_by=granted_by))
    await app_session.commit()
    return user, mint_token(org_id, subject=subject, email=user.email)


@pytest.mark.asyncio
async def test_create_list_update_delete_threshold(authed_org_and_project):
    org_id, project_id, _admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/admin/foresight-thresholds",
            headers=_headers(admin_token),
            json={"metric": "escalation_hours", "value": 12.0},
        )
        assert created.status_code == 201, created.text
        threshold_id = created.json()["id"]
        assert created.json()["organisation_id"] == str(org_id)
        assert created.json()["project_id"] is None

        listing = await client.get("/admin/foresight-thresholds", headers=_headers(admin_token))
        assert len(listing.json()) == 1

        updated = await client.patch(
            f"/admin/foresight-thresholds/{threshold_id}", headers=_headers(admin_token), json={"value": 6.0}
        )
        assert updated.status_code == 200
        assert updated.json()["value"] == 6.0

        deleted = await client.delete(
            f"/admin/foresight-thresholds/{threshold_id}", headers=_headers(admin_token)
        )
        assert deleted.status_code == 204

        after = await client.get("/admin/foresight-thresholds", headers=_headers(admin_token))
        assert after.json() == []


@pytest.mark.asyncio
async def test_resolve_threshold_prefers_project_specific_over_org_wide_default(app_session, org_and_project):
    """FR-FOR-07's most-specific-match resolution
    (app/foresight/threshold.py's resolve_threshold) — a project-specific
    row beats an org-wide default for the same metric."""
    from sqlalchemy import select

    from app.foresight.models import ForesightThreshold
    from app.foresight.threshold import resolve_threshold
    from app.models import Project

    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()

    app_session.add(
        ForesightThreshold(organisation_id=org_id, project_id=None, metric="silence_multiplier", value=3.0)
    )
    app_session.add(
        ForesightThreshold(
            organisation_id=org_id, project_id=project_id, metric="silence_multiplier", value=1.5
        )
    )
    await app_session.commit()

    value = await resolve_threshold(app_session, project=project, metric="silence_multiplier")
    assert value == 1.5


@pytest.mark.asyncio
async def test_resolve_threshold_falls_back_to_documented_default_when_unconfigured(app_session, org_and_project):
    from sqlalchemy import select

    from app.foresight.threshold import DEFAULT_THRESHOLDS, resolve_threshold
    from app.models import Project

    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()

    value = await resolve_threshold(app_session, project=project, metric="forecast_slack_days")
    assert value == DEFAULT_THRESHOLDS["forecast_slack_days"]


@pytest.mark.asyncio
async def test_non_administrator_cannot_manage_thresholds(app_session, authed_org_and_project):
    org_id, project_id, admin, _admin_token = authed_org_and_project
    _finance, finance_token = await _member(app_session, org_id, project_id, "finance", admin.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        listing = await client.get("/admin/foresight-thresholds", headers=_headers(finance_token))
        create = await client.post(
            "/admin/foresight-thresholds",
            headers=_headers(finance_token),
            json={"metric": "escalation_hours", "value": 1.0},
        )
    assert listing.status_code == 403
    assert create.status_code == 403


@pytest.mark.asyncio
async def test_set_and_read_quiet_hours(authed_org_and_project):
    org_id, project_id, _user, token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        before = await client.get(f"/projects/{project_id}/quiet-hours", headers=_headers(token))
        assert before.status_code == 200
        assert before.json() is None

        written = await client.put(
            f"/projects/{project_id}/quiet-hours",
            headers=_headers(token),
            json={
                "quiet_start_local": "22:00:00",
                "quiet_end_local": "07:00:00",
                "critical_severity_threshold": "critical",
            },
        )
        assert written.status_code == 200, written.text
        assert written.json()["quiet_start_local"] == "22:00:00"

        # PUT is idempotent — a second call with a different window updates
        # the same one-row-per-project config rather than creating a second.
        updated = await client.put(
            f"/projects/{project_id}/quiet-hours",
            headers=_headers(token),
            json={
                "quiet_start_local": "23:00:00",
                "quiet_end_local": "06:00:00",
                "critical_severity_threshold": "high",
            },
        )
        after = await client.get(f"/projects/{project_id}/quiet-hours", headers=_headers(token))

    assert updated.status_code == 200
    assert after.json()["quiet_start_local"] == "23:00:00"
    assert after.json()["critical_severity_threshold"] == "high"


@pytest.mark.asyncio
async def test_read_only_member_cannot_set_quiet_hours(app_session, authed_org_and_project):
    org_id, project_id, admin, _admin_token = authed_org_and_project
    _viewer, viewer_token = await _member(app_session, org_id, project_id, "read_only", admin.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        read = await client.get(f"/projects/{project_id}/quiet-hours", headers=_headers(viewer_token))
        write = await client.put(
            f"/projects/{project_id}/quiet-hours",
            headers=_headers(viewer_token),
            json={
                "quiet_start_local": "22:00:00",
                "quiet_end_local": "07:00:00",
                "critical_severity_threshold": "critical",
            },
        )
    assert read.status_code == 200
    assert write.status_code == 403


@pytest.mark.asyncio
async def test_thresholds_are_isolated_by_org(app_session, authed_org_and_project):
    org_id, project_id, _admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/admin/foresight-thresholds",
            headers=_headers(admin_token),
            json={"metric": "escalation_hours", "value": 1.0},
        )

    from app.foresight.models import ForesightThreshold
    from sqlalchemy import select

    await set_org_context(app_session, uuid.uuid4())  # a different, unrelated org
    visible = (await app_session.execute(select(ForesightThreshold))).scalars().all()
    assert visible == []
