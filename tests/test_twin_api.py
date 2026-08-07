"""REST + integration coverage for the Production Twin (PRD §6.8, FR-TWN):
archetype materialization at project creation, the milestones/dependencies
endpoints, the twin current/recompute/propagate/constraint endpoints, and the
recompute-on-commitment-transition wiring (app/twin/service.py). RLS and
role-gating are tested as two independent properties throughout, per
tests/test_membership_scoping.py's established split — RLS proven directly
against the database (mirroring test_rls.py), role-gating proven through the
real API (mirroring test_rbac.py).
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.identity.config import get_identity_settings
from app.identity.models import Membership, User
from app.ledger.extractor import _get_commitment_act_term
from app.models import Commitment, Deliverable, Evidence, Milestone, OntologyTerm, Organisation
from app.twin.models import Dependency, TwinAuditLog
from main import app
from tests.conftest import auth_headers, mint_token, set_org_context

DAY = timedelta(days=1)


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


async def _milestone_type_term(session, code: str) -> OntologyTerm:
    return (
        await session.execute(
            select(OntologyTerm).where(OntologyTerm.category == "milestone_type", OntologyTerm.code == code)
        )
    ).scalar_one()


async def _make_milestone(
    app_session, project_id, code, *, planned_at=None, actual_at=None, is_fixed=False, name=None
) -> Milestone:
    term = await _milestone_type_term(app_session, code)
    milestone = Milestone(
        project_id=project_id,
        type_term_id=term.id,
        name=name or code,
        planned_at=planned_at,
        actual_at=actual_at,
        is_fixed=is_fixed,
    )
    app_session.add(milestone)
    await app_session.flush()
    return milestone


async def _make_dependency(app_session, project_id, upstream, downstream, lag_days=0) -> Dependency:
    dependency = Dependency(
        project_id=project_id,
        upstream_milestone_id=upstream.id,
        downstream_milestone_id=downstream.id,
        lag_days=lag_days,
    )
    app_session.add(dependency)
    await app_session.flush()
    return dependency


# --- Archetype materialization at project creation (FR-TWN-02/09) ----------


@pytest.mark.asyncio
async def test_create_project_with_event_start_materializes_default_archetype(authed_org_and_project):
    org_id, _existing_project_id, user, token = authed_org_and_project
    event_start = datetime(2026, 6, 24, tzinfo=timezone.utc)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/projects",
            headers=_headers(token),
            json={"name": "Annex A event", "event_start": event_start.isoformat()},
        )
        assert created.status_code == 201, created.text
        project_id = created.json()["id"]

        milestones = await client.get(f"/projects/{project_id}/milestones", headers=_headers(token))
        dependencies = await client.get(
            f"/projects/{project_id}/milestones/dependencies", headers=_headers(token)
        )

    assert milestones.status_code == 200
    body = milestones.json()
    assert len(body) == 18  # CUE-PRD.md §4.2's full milestone_type vocabulary, one node each
    fixed = [m for m in body if m["is_fixed"]]
    assert len(fixed) == 1  # only 'doors' (FR-TWN-11)
    doors = fixed[0]
    assert doors["planned_at"] is not None
    assert datetime.fromisoformat(doors["planned_at"]) == event_start

    assert all(m["planned_at"] is not None for m in body)  # event_start was known, so every date resolves

    assert dependencies.status_code == 200
    assert len(dependencies.json()) == 17  # a linear chain over 18 nodes


@pytest.mark.asyncio
async def test_create_project_without_event_start_seeds_structure_with_null_dates(authed_org_and_project):
    """FR-TWN-01 says the graph must exist regardless; FR-TWN-02's dates just
    can't resolve yet without an event date to be relative to."""
    org_id, _existing_project_id, user, token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post("/projects", headers=_headers(token), json={"name": "No date yet"})
        project_id = created.json()["id"]
        milestones = await client.get(f"/projects/{project_id}/milestones", headers=_headers(token))

    body = milestones.json()
    assert len(body) == 18
    assert all(m["planned_at"] is None for m in body)
    assert any(m["is_fixed"] for m in body)  # is_fixed is structural, seeded regardless of dates


# --- Milestones/dependencies endpoints: PATCH is an audited PM override -----


@pytest.mark.asyncio
async def test_update_milestone_overrides_and_audits(app_session, authed_org_and_project):
    org_id, project_id, admin, admin_token = authed_org_and_project
    await set_org_context(app_session, org_id)
    milestone = await _make_milestone(app_session, project_id, "test_print", planned_at=None)
    await app_session.commit()

    new_planned_at = datetime(2026, 5, 31, tzinfo=timezone.utc)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.patch(
            f"/projects/{project_id}/milestones/{milestone.id}",
            headers=_headers(admin_token),
            json={"planned_at": new_planned_at.isoformat(), "is_fixed": True},
        )
    assert response.status_code == 200, response.text
    assert response.json()["is_fixed"] is True
    assert datetime.fromisoformat(response.json()["planned_at"]) == new_planned_at

    await set_org_context(app_session, org_id)
    audit_rows = (
        await app_session.execute(
            select(TwinAuditLog).where(
                TwinAuditLog.milestone_id == milestone.id, TwinAuditLog.action == "milestone_override"
            )
        )
    ).scalars().all()
    assert len(audit_rows) == 1
    changes = audit_rows[0].detail["changes"]
    assert changes["is_fixed"] == {"before": False, "after": True}
    assert changes["planned_at"]["after"] is not None


@pytest.mark.asyncio
async def test_update_milestone_read_only_member_is_forbidden(app_session, authed_org_and_project):
    org_id, project_id, admin, _admin_token = authed_org_and_project
    await set_org_context(app_session, org_id)
    milestone = await _make_milestone(app_session, project_id, "test_print")
    await app_session.commit()
    _viewer, viewer_token = await _member(app_session, org_id, project_id, "read_only", admin.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.patch(
            f"/projects/{project_id}/milestones/{milestone.id}",
            headers=_headers(viewer_token),
            json={"is_fixed": True},
        )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_read_milestone_not_found(authed_org_and_project):
    org_id, project_id, user, token = authed_org_and_project
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/projects/{project_id}/milestones/{uuid.uuid4()}", headers=_headers(token)
        )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_update_dependency_lag_days_overrides_and_audits(app_session, authed_org_and_project):
    org_id, project_id, admin, admin_token = authed_org_and_project
    await set_org_context(app_session, org_id)
    upstream = await _make_milestone(app_session, project_id, "artwork_submission")
    downstream = await _make_milestone(app_session, project_id, "test_print")
    dependency = await _make_dependency(app_session, project_id, upstream, downstream, lag_days=5)
    await app_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.patch(
            f"/projects/{project_id}/milestones/dependencies/{dependency.id}",
            headers=_headers(admin_token),
            json={"lag_days": 9},
        )
    assert response.status_code == 200, response.text
    assert response.json()["lag_days"] == 9

    await set_org_context(app_session, org_id)
    audit_rows = (
        await app_session.execute(
            select(TwinAuditLog).where(
                TwinAuditLog.dependency_id == dependency.id, TwinAuditLog.action == "dependency_override"
            )
        )
    ).scalars().all()
    assert len(audit_rows) == 1
    assert audit_rows[0].detail["changes"]["lag_days"] == {"before": 5, "after": 9}


@pytest.mark.asyncio
async def test_update_dependency_read_only_member_is_forbidden(app_session, authed_org_and_project):
    org_id, project_id, admin, _admin_token = authed_org_and_project
    await set_org_context(app_session, org_id)
    upstream = await _make_milestone(app_session, project_id, "artwork_submission")
    downstream = await _make_milestone(app_session, project_id, "test_print")
    dependency = await _make_dependency(app_session, project_id, upstream, downstream, lag_days=5)
    await app_session.commit()
    _viewer, viewer_token = await _member(app_session, org_id, project_id, "read_only", admin.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.patch(
            f"/projects/{project_id}/milestones/dependencies/{dependency.id}",
            headers=_headers(viewer_token),
            json={"lag_days": 1},
        )
    assert response.status_code == 403


# --- Twin computation endpoints ---------------------------------------------


async def _build_chain(app_session, project_id):
    """a -(5)-> b -(5)-> c(fixed). Exact lag match -> zero slack throughout,
    same shape as tests/test_twin_graph.py's linear-chain case."""
    base = datetime(2026, 6, 24, tzinfo=timezone.utc)
    a = await _make_milestone(app_session, project_id, "design_approval", planned_at=base - 10 * DAY)
    b = await _make_milestone(app_session, project_id, "artwork_submission", planned_at=base - 5 * DAY)
    c = await _make_milestone(app_session, project_id, "doors", planned_at=base, is_fixed=True)
    await _make_dependency(app_session, project_id, a, b, lag_days=5)
    await _make_dependency(app_session, project_id, b, c, lag_days=5)
    await app_session.commit()
    return a, b, c


@pytest.mark.asyncio
async def test_twin_current_returns_slack_and_critical_path(app_session, authed_org_and_project):
    org_id, project_id, admin, token = authed_org_and_project
    await set_org_context(app_session, org_id)
    a, b, c = await _build_chain(app_session, project_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/projects/{project_id}/twin/current", headers=_headers(token))
    assert response.status_code == 200, response.text
    body = response.json()
    by_id = {n["milestone_id"]: n for n in body["nodes"]}
    for node in (a, b, c):
        assert by_id[str(node.id)]["slack_days"] == 0.0
        assert by_id[str(node.id)]["is_critical"] is True
    assert set(body["critical_path"]) == {str(a.id), str(b.id), str(c.id)}
    assert body["binding_constraint"] in (str(a.id), str(b.id))  # never the fixed node


@pytest.mark.asyncio
async def test_twin_constraint_excludes_the_fixed_node(app_session, authed_org_and_project):
    org_id, project_id, admin, token = authed_org_and_project
    await set_org_context(app_session, org_id)
    a, b, c = await _build_chain(app_session, project_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/projects/{project_id}/twin/constraint", headers=_headers(token))
    assert response.status_code == 200, response.text
    assert response.json()["binding_constraint"] != str(c.id)
    assert response.json()["binding_constraint"] in (str(a.id), str(b.id))


@pytest.mark.asyncio
async def test_twin_recompute_writes_an_audit_entry(app_session, authed_org_and_project):
    org_id, project_id, admin, token = authed_org_and_project
    await set_org_context(app_session, org_id)
    await _build_chain(app_session, project_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(f"/projects/{project_id}/twin/recompute", headers=_headers(token))
    assert response.status_code == 200, response.text

    await set_org_context(app_session, org_id)
    rows = (
        await app_session.execute(
            select(TwinAuditLog).where(
                TwinAuditLog.project_id == project_id, TwinAuditLog.action == "recompute"
            )
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].detail["triggered_by"] == "manual"


@pytest.mark.asyncio
async def test_twin_propagate_reports_impact_and_stops_at_fixed_node(app_session, authed_org_and_project):
    org_id, project_id, admin, token = authed_org_and_project
    await set_org_context(app_session, org_id)
    a, b, c = await _build_chain(app_session, project_id)

    # Shift `a` 3 days later — pressure should reach `b`, then be absorbed by
    # the fixed `c` (doors) without moving it (FR-TWN-11).
    new_date = a.planned_at + 3 * DAY
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/twin/propagate",
            headers=_headers(token),
            json={"candidates": [{"milestone_id": str(a.id), "new_date": new_date.isoformat()}]},
        )
    assert response.status_code == 200, response.text
    result = response.json()["results"][0]
    by_id = {aff["milestone_id"]: aff for aff in result["affected"]}

    assert by_id[str(b.id)]["consumed_slack_days"] == pytest.approx(3.0)
    assert by_id[str(b.id)]["propagation_stopped"] is False
    assert by_id[str(c.id)]["propagation_stopped"] is True
    assert by_id[str(c.id)]["new_earliest"] == by_id[str(c.id)]["previous_earliest"]  # never moves


@pytest.mark.asyncio
async def test_twin_propagate_unknown_milestone_is_rejected(authed_org_and_project):
    org_id, project_id, admin, token = authed_org_and_project
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/twin/propagate",
            headers=_headers(token),
            json={
                "candidates": [
                    {"milestone_id": str(uuid.uuid4()), "new_date": datetime.now(timezone.utc).isoformat()}
                ]
            },
        )
    assert response.status_code == 422


# --- Recompute-on-commitment-transition (FR-TWN-04) -------------------------


async def _make_commitment_against_milestone(
    app_session, project_id, org_id, vendor, internal, milestone, state="committed"
) -> Commitment:
    deliverable_class = OntologyTerm(
        category="deliverable_class",
        code="test_led_screen",
        label_en="LED screen",
        label_zh="LED屏幕",
        active=True,
        effective_from=datetime.now(timezone.utc),
        version=1,
    )
    app_session.add(deliverable_class)
    await app_session.flush()

    deliverable = Deliverable(
        project_id=project_id, class_term_id=deliverable_class.id, milestone_id=milestone.id, name="LED screen"
    )
    app_session.add(deliverable)
    await app_session.flush()

    act_term = await _get_commitment_act_term(app_session, "confirm")
    commitment = Commitment(
        project_id=project_id,
        party_id=vendor.id,
        counterparty_id=internal.id,
        deliverable_id=deliverable.id,
        act_type_id=act_term.id,
        state=state,
        deliverable_en="LED screen install",
        deliverable_original="LED screen install",
        confidence=0.9,
        field_confidence={},
        verification_state="auto",
    )
    app_session.add(commitment)
    await app_session.flush()
    app_session.add(
        Evidence(
            commitment_id=commitment.id,
            channel="whatsapp",
            sent_at=datetime.now(timezone.utc),
            language="en",
            original_text="screens will be up on time",
            span_start=0,
            span_end=10,
        )
    )
    await app_session.commit()
    return commitment


@pytest.mark.asyncio
async def test_transition_to_delivered_sets_milestone_actual_at_and_recomputes(
    app_session, authed_org_and_project, parties
):
    org_id, project_id, admin, token = authed_org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)
    milestone = await _make_milestone(app_session, project_id, "test_print", planned_at=None)
    commitment = await _make_commitment_against_milestone(
        app_session, project_id, org_id, vendor, internal, milestone, state="committed"
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/commitments/{commitment.id}/transitions",
            headers=_headers(token),
            json={"to_state": "delivered"},
        )
    assert response.status_code == 200, response.text

    await set_org_context(app_session, org_id)
    # populate_existing=True: `milestone` is still in app_session's identity
    # map from creating it above (expire_on_commit=False), and a plain
    # select() won't overwrite an already-present, unexpired instance's
    # attributes from the freshly fetched row by default — the transition
    # happened through a different session/connection, so without this the
    # assertion below would see the stale, pre-transition in-memory object.
    refreshed = (
        await app_session.execute(
            select(Milestone).where(Milestone.id == milestone.id).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert refreshed.actual_at is not None

    audit_rows = (
        await app_session.execute(
            select(TwinAuditLog).where(
                TwinAuditLog.milestone_id == milestone.id, TwinAuditLog.action == "recompute"
            )
        )
    ).scalars().all()
    assert len(audit_rows) == 1
    assert audit_rows[0].detail["triggered_by_commitment_id"] == str(commitment.id)
    assert audit_rows[0].detail["milestone_actual_at_set"] is True


@pytest.mark.asyncio
async def test_transition_to_at_risk_does_not_move_milestone_dates(
    app_session, authed_org_and_project, parties
):
    """Deciding *how much* a milestone should slip from a broken/at-risk
    commitment is Foresight's job (FR-LCY-02/03), not this session's — see
    app/twin/service.py's recompute_on_commitment_transition docstring."""
    org_id, project_id, admin, token = authed_org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)
    milestone = await _make_milestone(app_session, project_id, "test_print", planned_at=None)
    commitment = await _make_commitment_against_milestone(
        app_session, project_id, org_id, vendor, internal, milestone, state="committed"
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/commitments/{commitment.id}/transitions",
            headers=_headers(token),
            json={"to_state": "at_risk"},
        )
    assert response.status_code == 200, response.text

    await set_org_context(app_session, org_id)
    refreshed = (
        await app_session.execute(
            select(Milestone).where(Milestone.id == milestone.id).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert refreshed.actual_at is None


@pytest.mark.asyncio
async def test_transition_with_no_linked_deliverable_does_not_error(authed_org_and_project, parties):
    """The common case — most commitments have no deliverable_id at all —
    must not be affected by this session's wiring."""
    org_id, project_id, admin, token = authed_org_and_project
    vendor, internal = parties
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            f"/projects/{project_id}/commitments",
            headers=_headers(token),
            json={
                "party_id": str(vendor.id),
                "counterparty_id": str(internal.id),
                "act_type": "commit",
                "deliverable_en": "LED screen install",
            },
        )
        commitment_id = created.json()["id"]
        response = await client.post(
            f"/projects/{project_id}/commitments/{commitment_id}/transitions",
            headers=_headers(token),
            json={"to_state": "committed"},
        )
    assert response.status_code == 200, response.text


# --- RLS, proven directly against the database (mirroring test_rls.py) -----


@pytest.mark.asyncio
async def test_milestones_isolated_via_project_join(app_session, authed_org_and_project):
    org_id, project_id, admin, _token = authed_org_and_project
    await set_org_context(app_session, org_id)
    milestone = await _make_milestone(app_session, project_id, "test_print")
    await app_session.commit()

    await set_org_context(app_session, uuid.uuid4())  # a different, unrelated org
    visible = (
        await app_session.execute(select(Milestone).where(Milestone.id == milestone.id))
    ).scalar_one_or_none()
    assert visible is None


@pytest.mark.asyncio
async def test_dependencies_isolated_via_project_join(app_session, authed_org_and_project):
    org_id, project_id, admin, _token = authed_org_and_project
    await set_org_context(app_session, org_id)
    upstream = await _make_milestone(app_session, project_id, "artwork_submission")
    downstream = await _make_milestone(app_session, project_id, "test_print")
    dependency = await _make_dependency(app_session, project_id, upstream, downstream, lag_days=1)
    await app_session.commit()

    await set_org_context(app_session, uuid.uuid4())
    visible = (
        await app_session.execute(select(Dependency).where(Dependency.id == dependency.id))
    ).scalar_one_or_none()
    assert visible is None


@pytest.mark.asyncio
async def test_twin_audit_log_isolated_via_project_join(app_session, authed_org_and_project):
    org_id, project_id, admin, token = authed_org_and_project
    await set_org_context(app_session, org_id)
    milestone = await _make_milestone(app_session, project_id, "test_print")
    await app_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.patch(
            f"/projects/{project_id}/milestones/{milestone.id}",
            headers=_headers(token),
            json={"is_fixed": True},
        )

    await set_org_context(app_session, uuid.uuid4())
    visible = (
        await app_session.execute(
            select(TwinAuditLog).where(TwinAuditLog.milestone_id == milestone.id)
        )
    ).scalars().all()
    assert visible == []


@pytest.mark.asyncio
async def test_milestones_are_isolated_by_org_through_the_api(app_session, authed_org_and_project):
    org_id, project_id, admin, token = authed_org_and_project

    other_org_id = uuid.uuid4()
    await set_org_context(app_session, other_org_id)
    app_session.add(Organisation(id=other_org_id, name="Other Org"))
    await app_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/projects/{project_id}/milestones", headers=auth_headers(other_org_id)
        )
    assert response.status_code == 404
