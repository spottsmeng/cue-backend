"""app/ask/summarise.py — FR-ASK-04/05's five grounded summary variants.
Exercises the composer functions directly against the real database (no
model calls involved at all in this module, so no fakes needed), same level
tests/test_reports_api.py exercises app/reports/composer.py at.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.ask.summarise import (
    compose_decision_history,
    compose_outstanding_actions,
    compose_project_status,
)
from app.ledger.audit import record_audit_event
from app.ledger.extractor import _get_commitment_act_term
from app.models import Commitment, Evidence, Project
from tests.conftest import set_org_context

NOW = datetime.now(timezone.utc)


async def _make_commitment(session, project_id, vendor, internal, *, state="committed", due_at=None) -> Commitment:
    act_term = await _get_commitment_act_term(session, "commit")
    commitment = Commitment(
        project_id=project_id, party_id=vendor.id, counterparty_id=internal.id, act_type_id=act_term.id,
        state=state, deliverable_en="LED screen install", due_at=due_at, confidence=0.9,
        verification_state="human_verified",
    )
    session.add(commitment)
    await session.flush()
    session.add(
        Evidence(
            commitment_id=commitment.id, channel="whatsapp", sent_at=NOW, language="en",
            original_text="confirmed",
        )
    )
    await session.commit()
    return commitment


@pytest.mark.asyncio
async def test_project_status_counts_open_and_at_risk_commitments(
    app_session, org_and_project, parties
):
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)

    await _make_commitment(app_session, project_id, vendor, internal, state="committed")
    await _make_commitment(app_session, project_id, vendor, internal, state="at_risk")
    await _make_commitment(app_session, project_id, vendor, internal, state="delivered")  # not open

    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    summary = await compose_project_status(app_session, project)

    assert summary.open_commitment_count.value == 2
    assert summary.at_risk_commitment_count.value == 1
    assert summary.project_name.value == project.name


@pytest.mark.asyncio
async def test_project_status_reports_no_upcoming_milestone(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()

    summary = await compose_project_status(app_session, project)

    assert summary.next_milestone_name.available is False
    assert summary.next_milestone_name.unavailable_reason is not None


@pytest.mark.asyncio
async def test_outstanding_actions_grouped_by_owner_and_due_window(
    app_session, org_and_project, parties
):
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)

    overdue = await _make_commitment(
        app_session, project_id, vendor, internal, due_at=NOW - timedelta(days=2)
    )
    soon = await _make_commitment(
        app_session, project_id, vendor, internal, due_at=NOW + timedelta(days=3)
    )
    no_date = await _make_commitment(app_session, project_id, vendor, internal, due_at=None)

    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    summary = await compose_outstanding_actions(app_session, project)

    assert len(summary.by_owner) == 1
    assert summary.by_owner[0].party_id == vendor.id
    assert {c.commitment_id for c in summary.by_owner[0].commitments} == {overdue.id, soon.id, no_date.id}

    windows = {w.window: {c.commitment_id for c in w.commitments} for w in summary.by_due_window}
    assert windows["overdue"] == {overdue.id}
    assert windows["due_within_7_days"] == {soon.id}
    assert windows["no_due_date"] == {no_date.id}


@pytest.mark.asyncio
async def test_decision_history_excludes_creation_events(
    app_session, org_and_project, parties, seeded_user
):
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)
    commitment = await _make_commitment(app_session, project_id, vendor, internal)

    await record_audit_event(
        app_session, project_id=project_id, commitment_id=commitment.id, action="created",
        actor_id=seeded_user.id,
    )
    await record_audit_event(
        app_session, project_id=project_id, commitment_id=commitment.id, action="state_transition",
        actor_id=seeded_user.id, from_state="committed", to_state="delivered",
    )
    await app_session.commit()

    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    summary = await compose_decision_history(app_session, project)

    assert len(summary.decisions) == 1
    assert summary.decisions[0].action == "state_transition"
