"""REST endpoints for /projects/{id}/deviations — list · create (manual,
FR-DEV-01) · confirm (auto-drafted two-step, FR-DEV-04) · resolve
(FR-DEV-03). RLS and role-gating tested as two independent properties.
"""

import uuid
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.foresight.deviation import draft_deviation
from app.foresight.models import Deviation, Notification
from app.foresight.notification import _event_type_for_notification
from app.identity.config import get_identity_settings
from app.identity.models import Membership, User
from app.models import Project
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


async def _make_auto_drafted_deviation(app_session, project_id) -> Deviation:
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    deviation = await draft_deviation(
        app_session, project=project, class_code="delay", description_en="Auto-detected delay",
        evidence_text="Detected by a test fixture standing in for a detector.",
    )
    await app_session.commit()
    return deviation


@pytest.mark.asyncio
async def test_manual_create_is_immediately_confirmed(authed_org_and_project):
    """FR-DEV-01: a PM logging one directly — nothing to confirm-or-edit
    about your own entry."""
    org_id, project_id, user, token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/deviations",
            headers=_headers(token),
            json={
                "class_code": "scope_change",
                "description_en": "Client added a second LED wall mid-build.",
                "original_text": "Client requested an additional LED wall on-site.",
            },
        )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "confirmed"
    assert body["confirmed_by"] == str(user.id)
    assert len(body["evidence"]) == 1


@pytest.mark.asyncio
async def test_manual_create_rejects_unknown_class_code(authed_org_and_project):
    org_id, project_id, _user, token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/deviations",
            headers=_headers(token),
            json={"class_code": "not_a_real_class", "description_en": "x", "original_text": "x"},
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_confirm_auto_drafted_deviation_without_corrections(app_session, authed_org_and_project):
    org_id, project_id, user, token = authed_org_and_project
    await set_org_context(app_session, org_id)
    deviation = await _make_auto_drafted_deviation(app_session, project_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/deviations/{deviation.id}/confirm", headers=_headers(token), json={}
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "confirmed"
    assert body["description_en"] == "Auto-detected delay"  # unchanged
    assert body["confirmed_by"] == str(user.id)


@pytest.mark.asyncio
async def test_confirm_with_correction_is_recorded_as_a_change(app_session, authed_org_and_project):
    org_id, project_id, _user, token = authed_org_and_project
    await set_org_context(app_session, org_id)
    deviation = await _make_auto_drafted_deviation(app_session, project_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/deviations/{deviation.id}/confirm",
            headers=_headers(token),
            json={"description_en": "Corrected: a two-week delay, not one."},
        )
    assert response.status_code == 200
    assert response.json()["description_en"] == "Corrected: a two-week delay, not one."


@pytest.mark.asyncio
async def test_resolve_deviation_sets_date_and_owner(app_session, authed_org_and_project):
    org_id, project_id, user, token = authed_org_and_project
    await set_org_context(app_session, org_id)
    deviation = await _make_auto_drafted_deviation(app_session, project_id)
    resolution_date = datetime.now(timezone.utc).isoformat()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/deviations/{deviation.id}/resolve",
            headers=_headers(token),
            json={"resolution_date": resolution_date, "resolution_owner": str(user.id)},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "resolved"
    assert body["resolution_owner"] == str(user.id)

    # Resolving twice is rejected, not silently re-applied.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        again = await client.post(
            f"/projects/{project_id}/deviations/{deviation.id}/resolve",
            headers=_headers(token),
            json={"resolution_date": resolution_date, "resolution_owner": str(user.id)},
        )
    assert again.status_code == 409



# --- Blind Spots item 1: deviation event dispatch --------------------------
# Each of the four paths a Deviation can be created/mutated through
# (draft_deviation's own auto-draft — the fix point shared by both real
# call sites, contradiction.py and forecast.py — plus the confirm and
# resolve endpoints) must leave behind a real Notification row naming this
# deviation, not just a status change with nothing downstream ever reading
# it. authed_org_and_project's own "administrator" membership is exactly
# the fallback recipient default_recipients (app/foresight/notification.py)
# resolves to when no project_manager exists yet, so no extra membership
# fixture is needed to get a real recipient.


@pytest.mark.asyncio
async def test_draft_deviation_dispatches_a_notification(app_session, authed_org_and_project):
    org_id, project_id, user, _token = authed_org_and_project
    await set_org_context(app_session, org_id)
    deviation = await _make_auto_drafted_deviation(app_session, project_id)

    notifications = (
        await app_session.execute(
            select(Notification).where(Notification.project_id == project_id)
        )
    ).scalars().all()
    assert len(notifications) == 1
    assert notifications[0].deviation_id == deviation.id
    assert notifications[0].recipient_id == user.id
    assert _event_type_for_notification(notifications[0]) == "deviation"


@pytest.mark.asyncio
async def test_confirm_deviation_endpoint_dispatches_a_notification(app_session, authed_org_and_project):
    org_id, project_id, user, token = authed_org_and_project
    await set_org_context(app_session, org_id)
    deviation = await _make_auto_drafted_deviation(app_session, project_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/deviations/{deviation.id}/confirm", headers=_headers(token), json={}
        )
    assert response.status_code == 200, response.text

    notification = (
        await app_session.execute(
            select(Notification).where(
                Notification.project_id == project_id, Notification.deviation_id == deviation.id
            )
        )
    ).scalar_one()
    assert notification.recipient_id == user.id
    assert _event_type_for_notification(notification) == "deviation"


@pytest.mark.asyncio
async def test_resolve_deviation_endpoint_dispatches_a_notification(app_session, authed_org_and_project):
    org_id, project_id, user, token = authed_org_and_project
    await set_org_context(app_session, org_id)
    deviation = await _make_auto_drafted_deviation(app_session, project_id)
    resolution_date = datetime.now(timezone.utc).isoformat()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/deviations/{deviation.id}/resolve",
            headers=_headers(token),
            json={"resolution_date": resolution_date, "resolution_owner": str(user.id)},
        )
    assert response.status_code == 200, response.text

    notification = (
        await app_session.execute(
            select(Notification).where(
                Notification.project_id == project_id, Notification.deviation_id == deviation.id
            )
        )
    ).scalar_one()
    assert notification.recipient_id == user.id
    assert _event_type_for_notification(notification) == "deviation"


@pytest.mark.asyncio
async def test_read_only_member_can_list_but_not_create(app_session, authed_org_and_project):
    org_id, project_id, admin, _admin_token = authed_org_and_project
    _viewer, viewer_token = await _member(app_session, org_id, project_id, "read_only", admin.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        listing = await client.get(f"/projects/{project_id}/deviations", headers=_headers(viewer_token))
        create = await client.post(
            f"/projects/{project_id}/deviations",
            headers=_headers(viewer_token),
            json={"class_code": "delay", "description_en": "x", "original_text": "x"},
        )
    assert listing.status_code == 200
    assert create.status_code == 403


@pytest.mark.asyncio
async def test_deviations_are_isolated_via_project_join_rls(app_session, authed_org_and_project):
    org_id, project_id, _user, _token = authed_org_and_project
    await set_org_context(app_session, org_id)
    deviation = await _make_auto_drafted_deviation(app_session, project_id)

    await set_org_context(app_session, uuid.uuid4())  # a different, unrelated org
    result = await app_session.execute(select(Deviation).where(Deviation.id == deviation.id))
    assert result.scalar_one_or_none() is None
