"""app/foresight/notification.py — FR-NTF-03 (collapsing) and FR-NTF-04
(quiet hours, with a live-event-window override for critical items).
"""

import uuid
from datetime import datetime, time, timedelta, timezone

import pytest
from sqlalchemy import select

from app.foresight.models import Notification, QuietHoursConfig, Risk
from app.foresight.notification import compute_deliverable_at, create_notification, dispatch_event
from app.identity.config import get_identity_settings
from app.identity.models import User
from app.models import Project
from tests.conftest import set_org_context

NOW = datetime.now(timezone.utc)


async def _make_user(app_session, org_id) -> User:
    subject = f"user-{uuid.uuid4()}"
    user = User(
        organisation_id=org_id, issuer=get_identity_settings().local_issuer,
        external_subject=subject, email=f"{subject}@example.test",
    )
    app_session.add(user)
    await app_session.commit()
    return user


@pytest.mark.asyncio
async def test_no_quiet_hours_config_means_immediately_deliverable(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()

    deliverable_at = await compute_deliverable_at(app_session, project, severity="low", now=NOW)
    assert deliverable_at == NOW


@pytest.mark.asyncio
async def test_notification_during_quiet_hours_is_deferred_to_quiet_hours_end(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    project.timezone = "UTC"
    app_session.add(
        QuietHoursConfig(
            project_id=project_id, quiet_start_local=time(22, 0), quiet_end_local=time(7, 0),
            critical_severity_threshold="critical",
        )
    )
    await app_session.commit()

    during_quiet_hours = NOW.replace(hour=23, minute=0, second=0, microsecond=0)
    deliverable_at = await compute_deliverable_at(app_session, project, severity="medium", now=during_quiet_hours)

    assert deliverable_at > during_quiet_hours
    assert deliverable_at.astimezone(timezone.utc).time() == time(7, 0)


@pytest.mark.asyncio
async def test_outside_quiet_hours_is_immediately_deliverable(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    project.timezone = "UTC"
    app_session.add(
        QuietHoursConfig(
            project_id=project_id, quiet_start_local=time(22, 0), quiet_end_local=time(7, 0),
            critical_severity_threshold="critical",
        )
    )
    await app_session.commit()

    midday = NOW.replace(hour=12, minute=0, second=0, microsecond=0)
    deliverable_at = await compute_deliverable_at(app_session, project, severity="low", now=midday)
    assert deliverable_at == midday


@pytest.mark.asyncio
async def test_critical_severity_bypasses_quiet_hours_during_live_event_window(app_session, org_and_project):
    """FR-NTF-04's own named override."""
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    project.timezone = "UTC"
    project.event_start = NOW - timedelta(days=1)
    project.event_end = NOW + timedelta(days=1)
    app_session.add(
        QuietHoursConfig(
            project_id=project_id, quiet_start_local=time(22, 0), quiet_end_local=time(7, 0),
            critical_severity_threshold="critical",
        )
    )
    await app_session.commit()

    during_quiet_hours = NOW.replace(hour=23, minute=0, second=0, microsecond=0)
    critical_deliverable_at = await compute_deliverable_at(
        app_session, project, severity="critical", now=during_quiet_hours
    )
    medium_deliverable_at = await compute_deliverable_at(
        app_session, project, severity="medium", now=during_quiet_hours
    )

    assert critical_deliverable_at == during_quiet_hours  # bypassed
    assert medium_deliverable_at > during_quiet_hours  # still deferred — below the configured threshold


@pytest.mark.asyncio
async def test_create_notification_collapses_a_second_risk_into_the_pending_one(app_session, org_and_project):
    """FR-NTF-03: a second finding for the same still-pending recipient
    collapses in, rather than firing a second notification."""
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    recipient = await _make_user(app_session, org_id)

    risk_a = Risk(
        project_id=project_id, source="silence", finding_key="silence:a", severity="medium",
        status="open", downstream_consequence="first finding",
    )
    risk_b = Risk(
        project_id=project_id, source="forecast", finding_key="forecast:b", severity="critical",
        status="open", downstream_consequence="second, more urgent finding",
    )
    app_session.add_all([risk_a, risk_b])
    await app_session.flush()

    first = await create_notification(
        app_session, project=project, recipient_id=recipient.id, severity="medium",
        downstream_consequence=risk_a.downstream_consequence, risk=risk_a,
    )
    await app_session.commit()
    second = await create_notification(
        app_session, project=project, recipient_id=recipient.id, severity="critical",
        downstream_consequence=risk_b.downstream_consequence, risk=risk_b,
    )
    await app_session.commit()

    assert first.id == second.id  # collapsed into the same row
    assert second.collapsed_count == 2
    assert risk_b.id in second.collapsed_risk_ids
    assert second.severity == "critical"  # escalated to the more urgent of the two
    assert second.downstream_consequence == risk_b.downstream_consequence

    all_notifications = (
        await app_session.execute(select(Notification).where(Notification.project_id == project_id))
    ).scalars().all()
    assert len(all_notifications) == 1


@pytest.mark.asyncio
async def test_create_notification_does_not_collapse_into_an_already_sent_one(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    recipient = await _make_user(app_session, org_id)
    risk_a = Risk(
        project_id=project_id, source="silence", finding_key="silence:x", severity="low",
        status="open", downstream_consequence="a",
    )
    risk_b = Risk(
        project_id=project_id, source="silence", finding_key="silence:y", severity="low",
        status="open", downstream_consequence="b",
    )
    app_session.add_all([risk_a, risk_b])
    await app_session.flush()

    first = await create_notification(
        app_session, project=project, recipient_id=recipient.id, severity="low",
        downstream_consequence="a", risk=risk_a,
    )
    first.sent_at = NOW
    first.delivered_via = "webhook"
    await app_session.commit()

    second = await create_notification(
        app_session, project=project, recipient_id=recipient.id, severity="low",
        downstream_consequence="b", risk=risk_b,
    )
    await app_session.commit()

    assert second.id != first.id

    all_notifications = (
        await app_session.execute(select(Notification).where(Notification.project_id == project_id))
    ).scalars().all()
    assert len(all_notifications) == 2


@pytest.mark.asyncio
async def test_dispatch_event_defaults_to_project_manager_recipients(app_session, authed_org_and_project):
    org_id, project_id, admin, _token = authed_org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()

    from app.identity.models import Membership

    pm = await _make_user(app_session, org_id)
    app_session.add(Membership(user_id=pm.id, project_id=project_id, role="project_manager", granted_by=admin.id))
    await app_session.commit()

    risk = Risk(
        project_id=project_id, source="silence", finding_key="silence:c", severity="high",
        status="open", downstream_consequence="test",
    )
    app_session.add(risk)
    await app_session.flush()

    notifications = await dispatch_event(app_session, project=project, event_type="risk", risk=risk)
    await app_session.commit()

    assert len(notifications) == 1
    assert notifications[0].recipient_id == pm.id
