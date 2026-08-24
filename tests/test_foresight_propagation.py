"""app/foresight/propagation.py — wires app/twin/graph.py's propagate() into
the automatic Risk/Deviation/Notification pipeline the moment a milestone is
genuinely rescheduled, closing the gap where downstream-impact detection
existed only behind the PM-triggered /twin/propagate what-if endpoint. Same
fixture style as test_foresight_forecast.py.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.foresight.models import Deviation, Notification, Risk
from app.foresight.propagation import record_milestone_delay
from app.models import Milestone, OntologyTerm, Organisation, Project
from app.models.vertical import Vertical
from app.twin.models import Dependency
from app.twin.service import build_graph
from tests.conftest import set_org_context

NOW = datetime.now(timezone.utc)


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


async def _setup_chain(app_session, project_id) -> tuple[Milestone, Milestone, Milestone]:
    """load_in -> rigging -> doors(fixed), 5 days of baseline slack on
    rigging — the same shape test_foresight_forecast.py's own fixtures use."""
    load_in = await _make_milestone(app_session, project_id, "load_in", planned_at=NOW + timedelta(days=5))
    rigging = await _make_milestone(app_session, project_id, "rigging", planned_at=NOW + timedelta(days=5))
    doors = await _make_milestone(
        app_session, project_id, "doors", planned_at=NOW + timedelta(days=10), is_fixed=True
    )
    app_session.add(
        Dependency(project_id=project_id, upstream_milestone_id=load_in.id, downstream_milestone_id=rigging.id, lag_days=0)
    )
    app_session.add(
        Dependency(project_id=project_id, upstream_milestone_id=rigging.id, downstream_milestone_id=doors.id, lag_days=0)
    )
    await app_session.commit()
    return load_in, rigging, doors


@pytest.mark.asyncio
async def test_delay_that_consumes_slack_creates_high_severity_propagation_risk(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    load_in, rigging, doors = await _setup_chain(app_session, project_id)

    baseline_nodes, baseline_edges = await build_graph(app_session, project_id)
    risk = await record_milestone_delay(
        app_session, project=project, milestone=load_in,
        baseline_nodes=baseline_nodes, baseline_edges=baseline_edges, new_date=NOW + timedelta(days=8),
    )
    await app_session.commit()

    assert risk is not None
    assert risk.source == "propagation"
    assert risk.severity == "high"
    assert risk.milestone_id == load_in.id

    affected_by_id = {a["milestone_id"]: a for a in risk.detail["affected"]}
    assert str(rigging.id) in affected_by_id
    assert str(doors.id) in affected_by_id
    assert affected_by_id[str(rigging.id)]["propagation_stopped"] is False
    assert affected_by_id[str(rigging.id)]["consumed_slack_days"] == pytest.approx(3.0)
    assert affected_by_id[str(doors.id)]["propagation_stopped"] is True
    # FR-FOR-09: never a bare alert — names the specific downstream milestone.
    assert "rigging" in risk.downstream_consequence


@pytest.mark.asyncio
async def test_delay_that_exhausts_slack_is_critical(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    load_in, rigging, doors = await _setup_chain(app_session, project_id)

    baseline_nodes, baseline_edges = await build_graph(app_session, project_id)
    risk = await record_milestone_delay(
        app_session, project=project, milestone=load_in,
        baseline_nodes=baseline_nodes, baseline_edges=baseline_edges, new_date=NOW + timedelta(days=11),
    )
    await app_session.commit()

    assert risk is not None
    assert risk.severity == "critical"


@pytest.mark.asyncio
async def test_delay_with_no_downstream_dependents_creates_no_risk(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    solo = await _make_milestone(app_session, project_id, "load_in", planned_at=NOW + timedelta(days=5))
    await app_session.commit()

    baseline_nodes, baseline_edges = await build_graph(app_session, project_id)
    risk = await record_milestone_delay(
        app_session, project=project, milestone=solo,
        baseline_nodes=baseline_nodes, baseline_edges=baseline_edges, new_date=NOW + timedelta(days=8),
    )
    assert risk is None

    remaining = (await app_session.execute(select(Risk).where(Risk.project_id == project_id))).scalars().all()
    assert remaining == []


@pytest.mark.asyncio
async def test_repeated_delay_supersedes_previous_open_propagation_risk(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    load_in, rigging, doors = await _setup_chain(app_session, project_id)
    baseline_nodes, baseline_edges = await build_graph(app_session, project_id)

    first = await record_milestone_delay(
        app_session, project=project, milestone=load_in,
        baseline_nodes=baseline_nodes, baseline_edges=baseline_edges, new_date=NOW + timedelta(days=8),
    )
    await app_session.commit()
    second = await record_milestone_delay(
        app_session, project=project, milestone=load_in,
        baseline_nodes=baseline_nodes, baseline_edges=baseline_edges, new_date=NOW + timedelta(days=9),
    )
    await app_session.commit()

    assert first is not None and second is not None
    assert first.id != second.id
    await app_session.refresh(first, attribute_names=["status", "superseded_by"])
    assert first.status == "superseded"
    assert first.superseded_by == second.id
    assert second.status == "open"

    open_risks = (
        await app_session.execute(
            select(Risk).where(
                Risk.project_id == project_id, Risk.source == "propagation", Risk.status == "open"
            )
        )
    ).scalars().all()
    assert len(open_risks) == 1
    assert open_risks[0].id == second.id


@pytest.mark.asyncio
async def test_creates_linked_delay_deviation(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    load_in, rigging, doors = await _setup_chain(app_session, project_id)

    baseline_nodes, baseline_edges = await build_graph(app_session, project_id)
    risk = await record_milestone_delay(
        app_session, project=project, milestone=load_in,
        baseline_nodes=baseline_nodes, baseline_edges=baseline_edges, new_date=NOW + timedelta(days=8),
    )
    await app_session.commit()

    deviation = (
        await app_session.execute(select(Deviation).where(Deviation.risk_id == risk.id))
    ).scalar_one()
    assert deviation.milestone_id == load_in.id
    assert deviation.status == "auto_drafted"

    class_term = (
        await app_session.execute(select(OntologyTerm).where(OntologyTerm.id == deviation.class_term_id))
    ).scalar_one()
    assert class_term.code == "delay"


@pytest.mark.asyncio
async def test_dispatches_notification_to_default_recipient(app_session, authed_org_and_project):
    org_id, project_id, user, _token = authed_org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    load_in, rigging, doors = await _setup_chain(app_session, project_id)

    baseline_nodes, baseline_edges = await build_graph(app_session, project_id)
    risk = await record_milestone_delay(
        app_session, project=project, milestone=load_in,
        baseline_nodes=baseline_nodes, baseline_edges=baseline_edges, new_date=NOW + timedelta(days=8),
    )
    await app_session.commit()

    # authed_org_and_project's user holds "administrator" — default_recipients
    # falls back to administrators when the project has no project_manager.
    notifications = (
        await app_session.execute(select(Notification).where(Notification.risk_id == risk.id))
    ).scalars().all()
    assert len(notifications) == 1
    assert notifications[0].recipient_id == user.id
    assert notifications[0].downstream_consequence == risk.downstream_consequence


@pytest.mark.asyncio
async def test_delay_on_a_vertical_with_no_seeded_deviation_class_still_raises_a_risk(app_session):
    """seed_data/deviation_classes.py's own docstring: deviation_class terms
    (including "delay") are currently only seeded for the event-production
    vertical — a second vertical is a named future roadmap item, not a
    hypothetical. This must not crash the milestone reschedule itself for a
    project on a vertical that hasn't seeded its own deviation_class terms
    yet: the Risk/Notification path is vertical-agnostic and has to survive
    on its own; only the optional Deviation link degrades away."""
    org_id = uuid.uuid4()
    await set_org_context(app_session, org_id)
    app_session.add(Organisation(id=org_id, name="Other Vertical Org"))
    await app_session.flush()

    # verticals is a PRESERVE_TABLES reference table (conftest.py never
    # truncates it between tests, same as ontology_terms/channel_types) — a
    # fixed code here would collide with itself on a second run against a
    # database that already has this test's leftover row.
    other_vertical_id = uuid.uuid4()
    app_session.add(
        Vertical(
            id=other_vertical_id,
            code=f"renovation-interior-design-{uuid.uuid4().hex[:8]}",
            name="Renovation / Interior Design",
        )
    )
    await app_session.flush()

    project_id = uuid.uuid4()
    app_session.add(
        Project(
            id=project_id, organisation_id=org_id, vertical_id=other_vertical_id,
            name="Reno Project", timezone="Asia/Singapore",
        )
    )
    await app_session.flush()

    # milestone_type IS seeded at the universal tier (vertical_id NULL), so
    # this part resolves regardless of vertical — only deviation_class isn't.
    term = (
        await app_session.execute(select(OntologyTerm).where(OntologyTerm.category == "milestone_type").limit(1))
    ).scalar_one()
    upstream = Milestone(project_id=project_id, type_term_id=term.id, name="task A", planned_at=NOW + timedelta(days=5))
    downstream = Milestone(project_id=project_id, type_term_id=term.id, name="task B", planned_at=NOW + timedelta(days=8))
    app_session.add_all([upstream, downstream])
    await app_session.flush()
    app_session.add(
        Dependency(project_id=project_id, upstream_milestone_id=upstream.id, downstream_milestone_id=downstream.id, lag_days=0)
    )
    await app_session.commit()

    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    baseline_nodes, baseline_edges = await build_graph(app_session, project_id)

    risk = await record_milestone_delay(
        app_session, project=project, milestone=upstream,
        baseline_nodes=baseline_nodes, baseline_edges=baseline_edges, new_date=NOW + timedelta(days=7),
    )
    await app_session.commit()

    assert risk is not None
    assert risk.source == "propagation"
    assert "task B" in risk.downstream_consequence

    deviations = (
        await app_session.execute(select(Deviation).where(Deviation.risk_id == risk.id))
    ).scalars().all()
    assert deviations == []
