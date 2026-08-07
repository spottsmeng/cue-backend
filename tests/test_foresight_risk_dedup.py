"""app/foresight/risk.py's create_or_supersede_risk — the single shared
FR-FOR-10 dedup helper every detector (silence.py, contradiction.py,
forecast.py) calls, per Prompt 7's own explicit instruction to put this
check in one place rather than three ad hoc ones.
"""

import pytest
from sqlalchemy import select

from app.foresight.models import ForesightAuditLog, Risk
from app.foresight.risk import create_or_supersede_risk, get_open_risk
from tests.conftest import set_org_context


@pytest.mark.asyncio
async def test_first_call_creates_a_new_open_risk(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)

    risk, created = await create_or_supersede_risk(
        app_session,
        project_id=project_id,
        source="forecast",
        finding_key="forecast:test-milestone",
        severity="high",
        downstream_consequence="Milestone X has 1.0 days of slack.",
    )
    await app_session.commit()

    assert created is True
    assert risk.status == "open"

    audit = (
        await app_session.execute(
            select(ForesightAuditLog).where(ForesightAuditLog.risk_id == risk.id)
        )
    ).scalars().all()
    assert [a.action for a in audit] == ["risk_created"]


@pytest.mark.asyncio
async def test_repeat_call_with_no_material_change_is_a_noop(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    kwargs = dict(
        project_id=project_id,
        source="forecast",
        finding_key="forecast:test-milestone",
        severity="high",
        downstream_consequence="Milestone X has 1.0 days of slack.",
        detail={"slack_days": 1.0},
    )

    first, created_first = await create_or_supersede_risk(app_session, **kwargs)
    await app_session.commit()
    second, created_second = await create_or_supersede_risk(app_session, **kwargs)
    await app_session.commit()

    assert created_first is True
    assert created_second is False
    assert first.id == second.id

    all_rows = (
        await app_session.execute(
            select(Risk).where(Risk.project_id == project_id, Risk.finding_key == "forecast:test-milestone")
        )
    ).scalars().all()
    assert len(all_rows) == 1


@pytest.mark.asyncio
async def test_material_change_supersedes_rather_than_duplicates(app_session, org_and_project):
    """A materially different finding for the same key (severity escalated)
    retires the old row and inserts a fresh one — 'supersede rather than
    duplicate' (item 4a), not silently mutate history in place."""
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)

    first, _ = await create_or_supersede_risk(
        app_session,
        project_id=project_id,
        source="silence",
        finding_key="silence:test-commitment",
        severity="medium",
        downstream_consequence="No response in 4 days.",
        detail={"gap_days": 4.0},
    )
    await app_session.commit()

    second, created = await create_or_supersede_risk(
        app_session,
        project_id=project_id,
        source="silence",
        finding_key="silence:test-commitment",
        severity="critical",
        downstream_consequence="No response in 12 days.",
        detail={"gap_days": 12.0},
    )
    await app_session.commit()

    assert created is True
    assert second.id != first.id

    await app_session.refresh(first)
    assert first.status == "superseded"
    assert first.superseded_by == second.id
    assert second.status == "open"

    still_open = await get_open_risk(
        app_session, project_id=project_id, source="silence", finding_key="silence:test-commitment"
    )
    assert still_open.id == second.id

    audit = (
        await app_session.execute(
            select(ForesightAuditLog).where(ForesightAuditLog.risk_id == first.id)
        )
    ).scalars().all()
    assert "risk_superseded" in [a.action for a in audit]
