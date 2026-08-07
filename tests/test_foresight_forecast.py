"""app/foresight/forecast.py — FR-FOR-06's documented heuristic (slack <=
threshold combined with zero/negative slack or an open Silence Radar flag)
and FR-LCY-03's due-time-passed at_risk -> broken sweep.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.foresight.forecast import scan_forecast, scan_overdue_commitments
from app.foresight.models import Deviation, Risk
from app.ledger.extractor import _get_commitment_act_term
from app.models import AuditLog, Commitment, Deliverable, Evidence, Milestone, OntologyTerm, Project
from app.twin.models import Dependency
from tests.conftest import set_org_context

NOW = datetime.now(timezone.utc)


async def _add_evidence(app_session, commitment_id) -> None:
    app_session.add(
        Evidence(
            commitment_id=commitment_id, channel="whatsapp", sent_at=NOW, language="en",
            original_text="confirmed",
        )
    )


async def _milestone_type_term(session, code: str) -> OntologyTerm:
    return (
        await session.execute(
            select(OntologyTerm).where(OntologyTerm.category == "milestone_type", OntologyTerm.code == code)
        )
    ).scalar_one()


async def _make_milestone(app_session, project_id, code, *, planned_at, is_fixed=False) -> Milestone:
    term = await _milestone_type_term(app_session, code)
    milestone = Milestone(
        project_id=project_id, type_term_id=term.id, name=code, planned_at=planned_at, is_fixed=is_fixed,
    )
    app_session.add(milestone)
    await app_session.flush()
    return milestone


async def _committed_commitment_for_milestone(app_session, project_id, vendor_id, internal_id, milestone_id) -> Commitment:
    deliverable_term = (
        await app_session.execute(
            select(OntologyTerm).where(OntologyTerm.category == "deliverable_class").limit(1)
        )
    ).scalar_one()
    deliverable = Deliverable(
        project_id=project_id, class_term_id=deliverable_term.id, milestone_id=milestone_id, name="test deliverable",
    )
    app_session.add(deliverable)
    await app_session.flush()

    act_term = await _get_commitment_act_term(app_session, "commit")
    commitment = Commitment(
        project_id=project_id, party_id=vendor_id, counterparty_id=internal_id, act_type_id=act_term.id,
        deliverable_id=deliverable.id, state="committed", deliverable_en="test deliverable",
        confidence=1.0, verification_state="human_verified",
    )
    app_session.add(commitment)
    await app_session.flush()
    await _add_evidence(app_session, commitment.id)
    await app_session.flush()
    return commitment


async def _open_silence_risk_for(app_session, project_id, commitment_id) -> Risk:
    risk = Risk(
        project_id=project_id, source="silence", finding_key=f"silence:{commitment_id}", severity="medium",
        status="open", commitment_id=commitment_id, downstream_consequence="test fixture silence risk",
    )
    app_session.add(risk)
    await app_session.flush()
    return risk


@pytest.mark.asyncio
async def test_zero_slack_milestone_without_silence_flag_is_high_severity(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()

    fixed = await _make_milestone(app_session, project_id, "doors", planned_at=NOW + timedelta(days=10), is_fixed=True)
    upstream = await _make_milestone(app_session, project_id, "load_in", planned_at=NOW + timedelta(days=10))
    app_session.add(Dependency(project_id=project_id, upstream_milestone_id=upstream.id, downstream_milestone_id=fixed.id, lag_days=0))
    await app_session.commit()

    risks = await scan_forecast(app_session, project)
    await app_session.commit()

    assert len(risks) == 1
    risk = risks[0]
    assert risk.source == "forecast"
    assert risk.severity == "high"
    assert risk.milestone_id == upstream.id
    assert risk.base_rate is None  # FR-FOR-06: no historical corpus — never fabricated
    assert risk.downstream_consequence


@pytest.mark.asyncio
async def test_fixed_milestone_itself_is_never_flagged(app_session, org_and_project):
    """A fixed node's slack is definitionally zero (app/twin/graph.py's
    forward/backward pass) — flagging it would be noise on every scan of
    every project, not a real signal."""
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()

    await _make_milestone(app_session, project_id, "doors", planned_at=NOW + timedelta(days=10), is_fixed=True)
    await app_session.commit()

    risks = await scan_forecast(app_session, project)
    assert risks == []


@pytest.mark.asyncio
async def test_positive_slack_with_silence_flag_is_flagged_high_not_critical(app_session, org_and_project, parties):
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()

    fixed = await _make_milestone(app_session, project_id, "doors", planned_at=NOW + timedelta(days=10), is_fixed=True)
    upstream = await _make_milestone(app_session, project_id, "load_in", planned_at=NOW + timedelta(days=8))
    app_session.add(Dependency(project_id=project_id, upstream_milestone_id=upstream.id, downstream_milestone_id=fixed.id, lag_days=0))
    await app_session.flush()

    commitment = await _committed_commitment_for_milestone(app_session, project_id, vendor.id, internal.id, upstream.id)
    await _open_silence_risk_for(app_session, project_id, commitment.id)
    await app_session.commit()

    risks = await scan_forecast(app_session, project)
    await app_session.commit()

    assert len(risks) == 1
    assert risks[0].severity == "high"
    assert risks[0].detail["silence_flag"] is True


@pytest.mark.asyncio
async def test_zero_slack_and_silence_flag_together_is_critical_and_transitions_commitment(
    app_session, org_and_project, parties
):
    """FR-LCY-02: forecast breach is the third named automatic
    committed -> at_risk trigger."""
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()

    fixed = await _make_milestone(app_session, project_id, "doors", planned_at=NOW + timedelta(days=10), is_fixed=True)
    upstream = await _make_milestone(app_session, project_id, "load_in", planned_at=NOW + timedelta(days=10))
    app_session.add(Dependency(project_id=project_id, upstream_milestone_id=upstream.id, downstream_milestone_id=fixed.id, lag_days=0))
    await app_session.flush()

    commitment = await _committed_commitment_for_milestone(app_session, project_id, vendor.id, internal.id, upstream.id)
    await _open_silence_risk_for(app_session, project_id, commitment.id)
    await app_session.commit()

    risks = await scan_forecast(app_session, project)
    await app_session.commit()

    assert len(risks) == 1
    assert risks[0].severity == "critical"

    await app_session.refresh(commitment)
    assert commitment.state == "at_risk"
    audit = (
        await app_session.execute(
            select(AuditLog).where(AuditLog.commitment_id == commitment.id, AuditLog.action == "state_transition")
        )
    ).scalars().all()
    assert len(audit) == 1
    assert audit[0].actor_id is None
    assert audit[0].detail["trigger"] == "forecast"


@pytest.mark.asyncio
async def test_ample_slack_is_not_flagged(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()

    fixed = await _make_milestone(app_session, project_id, "doors", planned_at=NOW + timedelta(days=30), is_fixed=True)
    upstream = await _make_milestone(app_session, project_id, "load_in", planned_at=NOW + timedelta(days=5))
    app_session.add(Dependency(project_id=project_id, upstream_milestone_id=upstream.id, downstream_milestone_id=fixed.id, lag_days=0))
    await app_session.commit()

    risks = await scan_forecast(app_session, project)
    assert risks == []


@pytest.mark.asyncio
async def test_scan_overdue_commitments_transitions_at_risk_to_broken_and_drafts_deviation(
    app_session, org_and_project, parties
):
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()

    act_term = await _get_commitment_act_term(app_session, "commit")
    commitment = Commitment(
        project_id=project_id, party_id=vendor.id, counterparty_id=internal.id, act_type_id=act_term.id,
        state="at_risk", deliverable_en="LED screen install", due_at=NOW - timedelta(days=1),
        confidence=1.0, verification_state="human_verified",
    )
    app_session.add(commitment)
    await app_session.flush()
    await _add_evidence(app_session, commitment.id)
    await app_session.commit()

    transitioned = await scan_overdue_commitments(app_session, project)
    await app_session.commit()

    assert len(transitioned) == 1
    await app_session.refresh(commitment)
    assert commitment.state == "broken"

    audit = (
        await app_session.execute(
            select(AuditLog).where(AuditLog.commitment_id == commitment.id, AuditLog.action == "state_transition")
        )
    ).scalars().all()
    assert audit[0].actor_id is None
    assert audit[0].from_state == "at_risk"
    assert audit[0].to_state == "broken"
    assert audit[0].detail["trigger"] == "due_time_passed"

    deviations = (
        await app_session.execute(select(Deviation).where(Deviation.commitment_id == commitment.id))
    ).scalars().all()
    assert len(deviations) == 1
    assert deviations[0].status == "auto_drafted"


@pytest.mark.asyncio
async def test_scan_overdue_commitments_ignores_commitments_not_yet_due(app_session, org_and_project, parties):
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()

    act_term = await _get_commitment_act_term(app_session, "commit")
    commitment = Commitment(
        project_id=project_id, party_id=vendor.id, counterparty_id=internal.id, act_type_id=act_term.id,
        state="at_risk", deliverable_en="LED screen install", due_at=NOW + timedelta(days=1),
        confidence=1.0, verification_state="human_verified",
    )
    app_session.add(commitment)
    await app_session.flush()
    await _add_evidence(app_session, commitment.id)
    await app_session.commit()

    transitioned = await scan_overdue_commitments(app_session, project)
    assert transitioned == []
