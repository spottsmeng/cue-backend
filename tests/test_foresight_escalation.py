"""app/foresight/escalation.py — FR-FOR-08 (severity-based escalation
chains with delegation-aware routing) and FR-NTF-05 (escalate unacknowledged
critical items). Extends tests/test_delegation.py's own `_member`/delegation
fixture shape (per Prompt 7's testing expectation) rather than rebuilding it.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.foresight.escalation import ESCALATION_CHAIN, escalate_unacknowledged_risks
from app.foresight.models import ForesightThreshold, Notification, Risk
from app.identity.config import get_identity_settings
from app.identity.models import Delegation, Membership, User
from app.models import Project
from tests.conftest import set_org_context

NOW = datetime.now(timezone.utc)


async def _member(app_session, org_id, project_id, role, granted_by):
    """Mirrors tests/test_delegation.py's own _member helper exactly."""
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
    return user


async def _make_stale_open_risk(app_session, project_id, *, created_at, escalated_to_role=None, escalated_at=None) -> Risk:
    risk = Risk(
        project_id=project_id, source="silence", finding_key=f"silence:{uuid.uuid4()}", severity="high",
        status="open", downstream_consequence="test fixture risk", created_at=created_at,
        escalated_to_role=escalated_to_role, escalated_at=escalated_at,
    )
    app_session.add(risk)
    await app_session.commit()
    return risk


@pytest.mark.asyncio
async def test_risk_not_yet_past_threshold_is_not_escalated(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    risk = await _make_stale_open_risk(app_session, project_id, created_at=NOW - timedelta(hours=1))

    escalated = await escalate_unacknowledged_risks(app_session, project)
    assert escalated == []
    await app_session.refresh(risk)
    assert risk.escalated_to_role is None


@pytest.mark.asyncio
async def test_escalates_to_first_chain_tier_and_notifies_role_holder(app_session, authed_org_and_project):
    org_id, project_id, admin, _token = authed_org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    pm = await _member(app_session, org_id, project_id, "project_manager", admin.id)
    risk = await _make_stale_open_risk(app_session, project_id, created_at=NOW - timedelta(hours=25))

    escalated = await escalate_unacknowledged_risks(app_session, project)
    await app_session.commit()

    assert len(escalated) == 1
    await app_session.refresh(risk)
    assert risk.escalated_to_role == ESCALATION_CHAIN[0] == "project_manager"
    assert risk.escalated_at is not None

    notifications = (
        await app_session.execute(
            select(Notification).where(Notification.risk_id == risk.id, Notification.recipient_id == pm.id)
        )
    ).scalars().all()
    assert len(notifications) == 1
    assert notifications[0].downstream_consequence  # FR-NTF-02


@pytest.mark.asyncio
async def test_escalation_routes_through_an_active_delegation_not_only_membership(
    app_session, authed_org_and_project
):
    """FR-FOR-08's "delegation-aware routing" — a user with no standing
    `producer` membership but an active Delegation for that role still
    receives the escalation notification, exactly the property
    tests/test_delegation.py's own tests exercise for ordinary write access."""
    org_id, project_id, admin, _token = authed_org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()

    delegate_subject = f"delegate-{uuid.uuid4()}"
    delegate = User(
        organisation_id=org_id, issuer=get_identity_settings().local_issuer,
        external_subject=delegate_subject, email=f"{delegate_subject}@example.test",
    )
    app_session.add(delegate)
    await app_session.flush()
    app_session.add(
        Delegation(
            project_id=project_id, delegator_id=admin.id, delegate_id=delegate.id, role="producer",
            granted_by=admin.id, expires_at=NOW + timedelta(hours=2),
        )
    )
    await app_session.commit()

    # Already escalated to project_manager over 24h ago (no PM exists to
    # notify — fine, escalation still advances the chain), due for the next
    # tier.
    risk = await _make_stale_open_risk(
        app_session, project_id, created_at=NOW - timedelta(hours=50),
        escalated_to_role="project_manager", escalated_at=NOW - timedelta(hours=25),
    )

    await escalate_unacknowledged_risks(app_session, project)
    await app_session.commit()

    await app_session.refresh(risk)
    assert risk.escalated_to_role == "producer"

    notifications = (
        await app_session.execute(
            select(Notification).where(Notification.risk_id == risk.id, Notification.recipient_id == delegate.id)
        )
    ).scalars().all()
    assert len(notifications) == 1


@pytest.mark.asyncio
async def test_risk_at_top_of_chain_is_not_escalated_further(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    risk = await _make_stale_open_risk(
        app_session, project_id, created_at=NOW - timedelta(hours=100),
        escalated_to_role=ESCALATION_CHAIN[-1], escalated_at=NOW - timedelta(hours=50),
    )

    escalated = await escalate_unacknowledged_risks(app_session, project)
    assert escalated == []
    await app_session.refresh(risk)
    assert risk.escalated_to_role == ESCALATION_CHAIN[-1]


@pytest.mark.asyncio
async def test_acknowledged_risks_are_never_escalated(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    risk = Risk(
        project_id=project_id, source="forecast", finding_key=f"forecast:{uuid.uuid4()}", severity="high",
        status="acknowledged", downstream_consequence="test fixture", created_at=NOW - timedelta(hours=100),
    )
    app_session.add(risk)
    await app_session.commit()

    escalated = await escalate_unacknowledged_risks(app_session, project)
    assert escalated == []


@pytest.mark.asyncio
async def test_escalation_hours_threshold_is_configurable_per_project(app_session, org_and_project):
    """FR-FOR-07: a project can tighten the default 24h escalation window."""
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    app_session.add(
        ForesightThreshold(organisation_id=org_id, project_id=project_id, metric="escalation_hours", value=1.0)
    )
    await app_session.commit()

    risk = await _make_stale_open_risk(app_session, project_id, created_at=NOW - timedelta(hours=2))
    escalated = await escalate_unacknowledged_risks(app_session, project)
    assert len(escalated) == 1
    await app_session.refresh(risk)
    assert risk.escalated_to_role == "project_manager"
