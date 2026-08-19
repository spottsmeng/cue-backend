"""app/api/layer_a_admin.py — the Layer A observability proxy: RBAC
(administrator-only, same require_org_administrator tier app/api/admin.py's
org-wide surfaces already use), the DB-backed trend/alert/config endpoints,
and RLS isolation between organisations. The live-proxy endpoints
(/accounts, /conflicts/live) are exercised only for their unconfigured-503
path here — a real Layer A process is what app/layer_a/poller.py's own
real-fixture-server test (test_layer_a_poller.py) is for.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.identity.config import get_identity_settings
from app.identity.models import Membership, User
from app.layer_a.models import LayerAAlert, LayerAAlertConfig, LayerAAlertDelivery, LayerAHealthSnapshot
from main import app
from tests.conftest import mint_token, set_org_context

NOW = datetime.now(timezone.utc)


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
async def test_non_administrator_denied_from_every_layer_a_route(app_session, authed_org_and_project):
    org_id, project_id, admin, _admin_token = authed_org_and_project
    _designer, designer_token = await _member(app_session, org_id, project_id, "designer", admin.id)
    alert_id = uuid.uuid4()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/admin/layer-a/accounts", headers=_headers(designer_token))).status_code == 403
        assert (await client.get("/admin/layer-a/conflicts", headers=_headers(designer_token))).status_code == 403
        assert (await client.get("/admin/layer-a/alerts", headers=_headers(designer_token))).status_code == 403
        assert (await client.get("/admin/layer-a/config", headers=_headers(designer_token))).status_code == 403
        assert (await client.put("/admin/layer-a/config", json={}, headers=_headers(designer_token))).status_code == 403
        assert (
            await client.post(f"/admin/layer-a/alerts/{alert_id}/acknowledge", headers=_headers(designer_token))
        ).status_code == 403


@pytest.mark.asyncio
async def test_live_account_proxy_503s_when_layer_a_unconfigured(authed_org_and_project):
    _org_id, _project_id, _admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/admin/layer-a/accounts", headers=_headers(admin_token))

    assert response.status_code == 503


@pytest.mark.asyncio
async def test_get_config_creates_a_default_row_on_first_read(authed_org_and_project):
    _org_id, _project_id, _admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/admin/layer-a/config", headers=_headers(admin_token))

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["sustained_disconnect_minutes"] == 5
    assert body["reconnect_attempt_threshold"] == 5
    assert body["reconnect_attempt_window_minutes"] == 10
    assert body["webhook_configured"] is False
    assert "webhook_secret" not in body


@pytest.mark.asyncio
async def test_put_config_updates_fields_and_never_returns_the_secret(authed_org_and_project):
    _org_id, _project_id, _admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/admin/layer-a/config",
            json={
                "enabled": True,
                "sustained_disconnect_minutes": 10,
                "webhook_url": "https://example.test/hook",
                "webhook_enabled": True,
                "email_recipients": ["ops@example.test"],
                "email_enabled": True,
            },
            headers=_headers(admin_token),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["sustained_disconnect_minutes"] == 10
    assert body["reconnect_attempt_threshold"] == 5  # untouched field keeps its default
    assert body["webhook_url"] == "https://example.test/hook"
    assert body["webhook_configured"] is True
    assert body["email_recipients"] == ["ops@example.test"]
    assert "webhook_secret" not in body


@pytest.mark.asyncio
async def test_put_config_only_regenerates_secret_when_webhook_url_actually_changes(
    app_session, authed_org_and_project
):
    org_id, _project_id, _admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.put(
            "/admin/layer-a/config", json={"webhook_url": "https://example.test/hook"}, headers=_headers(admin_token)
        )
        await set_org_context(app_session, org_id)
        first_secret = (
            await app_session.execute(
                select(LayerAAlertConfig.webhook_secret).where(
                    LayerAAlertConfig.organisation_id == org_id
                )
            )
        ).scalar_one()

        # Re-PUT with the *same* URL plus an unrelated field change.
        await client.put(
            "/admin/layer-a/config",
            json={"webhook_url": "https://example.test/hook", "sustained_disconnect_minutes": 7},
            headers=_headers(admin_token),
        )
        await set_org_context(app_session, org_id)
        second_secret = (
            await app_session.execute(
                select(LayerAAlertConfig.webhook_secret).where(
                    LayerAAlertConfig.organisation_id == org_id
                )
            )
        ).scalar_one()

    assert first_secret is not None
    assert second_secret == first_secret


@pytest.mark.asyncio
async def test_config_is_isolated_by_organisation(app_session, authed_org_and_project, seeded_vertical_id):
    org_id, _project_id, admin, admin_token = authed_org_and_project

    other_org_id = uuid.uuid4()
    other_project_id = uuid.uuid4()
    await set_org_context(app_session, other_org_id)
    from app.models import Organisation, Project

    app_session.add(Organisation(id=other_org_id, name="Other Org"))
    await app_session.flush()
    app_session.add(
        Project(
            id=other_project_id, organisation_id=other_org_id, vertical_id=seeded_vertical_id,
            name="Other Project", timezone="Asia/Singapore",
        )
    )
    await app_session.flush()
    other_subject = f"admin-{uuid.uuid4()}"
    other_user = User(
        organisation_id=other_org_id, issuer=get_identity_settings().local_issuer,
        external_subject=other_subject, email=f"{other_subject}@example.test",
    )
    app_session.add(other_user)
    await app_session.flush()
    app_session.add(
        Membership(user_id=other_user.id, project_id=other_project_id, role="administrator", granted_by=other_user.id)
    )
    await app_session.commit()
    other_token = mint_token(other_org_id, subject=other_subject, email=other_user.email)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.put(
            "/admin/layer-a/config", json={"sustained_disconnect_minutes": 42}, headers=_headers(admin_token)
        )
        other_response = await client.get("/admin/layer-a/config", headers=_headers(other_token))

    assert other_response.status_code == 200
    # A brand-new default row for the other org, not org_id's 42-minute one.
    assert other_response.json()["sustained_disconnect_minutes"] == 5


@pytest.mark.asyncio
async def test_account_trend_returns_seeded_snapshots_newest_first(app_session, authed_org_and_project):
    org_id, _project_id, _admin, admin_token = authed_org_and_project
    await set_org_context(app_session, org_id)
    for i, status in enumerate(["connecting", "connected", "disconnected"]):
        app_session.add(
            LayerAHealthSnapshot(
                organisation_id=org_id, account_id="acct-1", source="transition",
                recorded_at=NOW - timedelta(minutes=10 - i), status=status, connect_attempts=0,
            )
        )
    await app_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/admin/layer-a/accounts/acct-1/trend", headers=_headers(admin_token))

    assert response.status_code == 200
    body = response.json()
    assert [row["status"] for row in body] == ["disconnected", "connected", "connecting"]


@pytest.mark.asyncio
async def test_acknowledge_resolves_session_conflict_but_not_sustained_disconnect(
    app_session, authed_org_and_project
):
    org_id, _project_id, admin, admin_token = authed_org_and_project
    await set_org_context(app_session, org_id)
    conflict_alert = LayerAAlert(
        organisation_id=org_id, alert_type="session_conflict", account_id=None,
        severity="critical", state="open", condition_detail={"refused_pid": 1, "owner_pid": 2},
    )
    disconnect_alert = LayerAAlert(
        organisation_id=org_id, alert_type="sustained_disconnect", account_id="acct-1",
        severity="serious", state="open", condition_detail={"duration_minutes": 9},
    )
    app_session.add_all([conflict_alert, disconnect_alert])
    await app_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        conflict_res = await client.post(
            f"/admin/layer-a/alerts/{conflict_alert.id}/acknowledge", headers=_headers(admin_token)
        )
        disconnect_res = await client.post(
            f"/admin/layer-a/alerts/{disconnect_alert.id}/acknowledge", headers=_headers(admin_token)
        )

    assert conflict_res.status_code == 200
    assert conflict_res.json()["state"] == "resolved"
    assert conflict_res.json()["acknowledged_by"] == str(admin.id)

    assert disconnect_res.status_code == 200
    assert disconnect_res.json()["state"] == "open"
    assert disconnect_res.json()["acknowledged_by"] == str(admin.id)


@pytest.mark.asyncio
async def test_alert_deliveries_are_listed_for_the_right_alert(app_session, authed_org_and_project):
    org_id, _project_id, _admin, admin_token = authed_org_and_project
    await set_org_context(app_session, org_id)
    alert = LayerAAlert(
        organisation_id=org_id, alert_type="reconnect_flapping", account_id="acct-1",
        severity="serious", state="open", condition_detail={},
    )
    app_session.add(alert)
    await app_session.flush()
    app_session.add_all(
        [
            LayerAAlertDelivery(alert_id=alert.id, organisation_id=org_id, channel="banner", success=True),
            LayerAAlertDelivery(
                alert_id=alert.id, organisation_id=org_id, channel="webhook", success=False, detail="timed out"
            ),
        ]
    )
    await app_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/admin/layer-a/alerts/{alert.id}/deliveries", headers=_headers(admin_token))

    assert response.status_code == 200
    channels = {row["channel"] for row in response.json()}
    assert channels == {"banner", "webhook"}


@pytest.mark.asyncio
async def test_open_alert_count_reflects_only_open_state(app_session, authed_org_and_project):
    org_id, _project_id, _admin, admin_token = authed_org_and_project
    await set_org_context(app_session, org_id)
    app_session.add_all(
        [
            LayerAAlert(
                organisation_id=org_id, alert_type="sustained_disconnect", account_id="acct-1",
                severity="serious", state="open", condition_detail={},
            ),
            LayerAAlert(
                organisation_id=org_id, alert_type="reconnect_flapping", account_id="acct-2",
                severity="serious", state="resolved", condition_detail={}, resolved_at=NOW,
            ),
        ]
    )
    await app_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/admin/layer-a/alerts/open/count", headers=_headers(admin_token))

    assert response.status_code == 200
    assert response.json() == {"count": 1}
