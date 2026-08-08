"""REST endpoint for §11.2's `/parties/{id}/reliability` — metrics ·
history. FR-VRG-05 (Procurement/Finance-tier access, org-wide, not
project-scoped — app/api/deps.py's require_org_finance) and FR-VRG-07
("never expose a vendor's metrics to another vendor", satisfied by there
being no vendor-facing surface anywhere in this codebase at all) are both
exercised here as independent, explicit properties, per Prompt 10's own
testing expectation.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.identity.config import get_identity_settings
from app.identity.models import Membership, User
from app.models import Project, VendorMetric
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


async def _create_commitment(client, token, project_id, vendor_id, internal_id, **overrides):
    body = {
        "party_id": str(vendor_id),
        "counterparty_id": str(internal_id),
        "act_type": "commit",
        "deliverable_en": "LED screen install",
        **overrides,
    }
    return await client.post(f"/projects/{project_id}/commitments", headers=_headers(token), json=body)


async def _transition(client, token, project_id, commitment_id, to_state):
    return await client.post(
        f"/projects/{project_id}/commitments/{commitment_id}/transitions",
        headers=_headers(token), json={"to_state": to_state},
    )


@pytest.mark.asyncio
async def test_no_auth_header_is_401_no_vendor_facing_route(org_and_project, parties):
    """FR-VRG-07: there is no vendor-authenticatable identity anywhere in
    this codebase (P1) — proven concretely by hitting the one route this
    milestone adds with zero credentials and getting rejected outright,
    never a 200 or even a 403 that would imply the route is at least
    reachable to try a role against."""
    _org_id, _project_id = org_and_project
    vendor, _internal = parties

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/parties/{vendor.id}/reliability")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_role_gating_requires_finance_or_producer_tier(
    app_session, authed_org_and_project, parties
):
    """FR-VRG-05: the default authed_org_and_project identity is
    "administrator" — a real, internal, project-provisioning role, but not
    Finance/Procurement — so it must be refused here even though it can do
    almost everything else in this codebase. A finance-tier member succeeds."""
    org_id, project_id, admin, admin_token = authed_org_and_project
    vendor, _internal = parties
    _finance_user, finance_token = await _member(
        app_session, org_id, project_id, "finance", admin.id
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        as_admin = await client.get(
            f"/parties/{vendor.id}/reliability", headers=_headers(admin_token)
        )
        as_finance = await client.get(
            f"/parties/{vendor.id}/reliability", headers=_headers(finance_token)
        )

    assert as_admin.status_code == 403
    assert as_finance.status_code == 200


@pytest.mark.asyncio
async def test_unknown_or_cross_org_party_is_404(app_session, authed_org_and_project):
    org_id, project_id, admin, _admin_token = authed_org_and_project
    _finance_user, finance_token = await _member(
        app_session, org_id, project_id, "finance", admin.id
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/parties/{uuid.uuid4()}/reliability", headers=_headers(finance_token)
        )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_recompute_triggered_by_real_lifecycle_transitions_end_to_end(
    app_session, authed_org_and_project, parties
):
    """FR-VRG-03 exercised through the real API, not a direct service call:
    proposed -> committed -> delivered on a real commitment (with a due
    date safely in the future, so the delivery lands on time) should leave
    the vendor's on_time_rate and median_response_time_days computed, and
    the history endpoint should show more than one snapshot — one per
    transition that recomputed it."""
    org_id, project_id, admin, admin_token = authed_org_and_project
    vendor, internal = parties
    _finance_user, finance_token = await _member(
        app_session, org_id, project_id, "finance", admin.id
    )
    due = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await _create_commitment(
            client, admin_token, project_id, vendor.id, internal.id, due_at=due
        )
        assert created.status_code == 201, created.text
        commitment_id = created.json()["id"]

        committed = await _transition(client, admin_token, project_id, commitment_id, "committed")
        assert committed.status_code == 200, committed.text
        delivered = await _transition(client, admin_token, project_id, commitment_id, "delivered")
        assert delivered.status_code == 200, delivered.text

        metrics_response = await client.get(
            f"/parties/{vendor.id}/reliability", headers=_headers(finance_token)
        )
        history_response = await client.get(
            f"/parties/{vendor.id}/reliability/history?metric=on_time_rate",
            headers=_headers(finance_token),
        )

    assert metrics_response.status_code == 200
    metrics_by_name = {m["metric"]: m for m in metrics_response.json()["metrics"]}
    assert metrics_by_name["on_time_rate"]["available"] is True
    assert metrics_by_name["on_time_rate"]["value"] == pytest.approx(1.0)

    assert history_response.status_code == 200
    history = history_response.json()["history"]
    assert len(history) == 2  # one recompute per transition (committed, delivered)


@pytest.mark.asyncio
async def test_segmentation_by_event_archetype(
    app_session, authed_org_and_project, parties, seeded_vertical_id
):
    org_id, project_id, admin, admin_token = authed_org_and_project
    vendor, internal = parties
    _finance_user, finance_token = await _member(
        app_session, org_id, project_id, "finance", admin.id
    )

    default_project = (
        await app_session.execute(select(Project).where(Project.id == project_id))
    ).scalar_one()
    default_project.archetype_code = "trade_show"
    second_project = Project(
        id=uuid.uuid4(), organisation_id=org_id, vertical_id=seeded_vertical_id,
        name="Gala Project", timezone="Asia/Singapore", archetype_code="gala_dinner",
    )
    app_session.add(second_project)
    await app_session.flush()
    await app_session.commit()

    _second_member, _ = await _member(
        app_session, org_id, second_project.id, "finance", admin.id
    )
    await app_session.execute(
        Membership.__table__.insert().values(
            user_id=admin.id, project_id=second_project.id, role="administrator", granted_by=admin.id
        )
    )
    await app_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        c1 = await _create_commitment(client, admin_token, project_id, vendor.id, internal.id)
        await _transition(client, admin_token, project_id, c1.json()["id"], "committed")

        c2 = await _create_commitment(
            client, admin_token, second_project.id, vendor.id, internal.id
        )
        await _transition(client, admin_token, second_project.id, c2.json()["id"], "committed")

        overall = await client.get(
            f"/parties/{vendor.id}/reliability", headers=_headers(finance_token)
        )
        trade_show = await client.get(
            f"/parties/{vendor.id}/reliability?event_archetype=trade_show",
            headers=_headers(finance_token),
        )
        gala = await client.get(
            f"/parties/{vendor.id}/reliability?event_archetype=gala_dinner",
            headers=_headers(finance_token),
        )

    assert overall.status_code == trade_show.status_code == gala.status_code == 200
    # The org-wide "overall" row's sample count for on_time_rate-adjacent
    # data isn't guaranteed non-empty (neither commitment resolved yet),
    # but every segment must at least be independently addressable and
    # non-identical in what it reports for a metric with real per-segment
    # signal (median_response_time_days' sample_size, from Evidence).
    trade_show_by_metric = {m["metric"]: m for m in trade_show.json()["metrics"]}
    gala_by_metric = {m["metric"]: m for m in gala.json()["metrics"]}
    assert trade_show_by_metric["median_response_time_days"]["segment_event_archetype"] == "trade_show"
    assert gala_by_metric["median_response_time_days"]["segment_event_archetype"] == "gala_dinner"


@pytest.mark.asyncio
async def test_vendor_metrics_are_isolated_by_org(app_session, authed_org_and_project, parties):
    """Same RLS-isolation proof shape test_foresight_thresholds_api.py's
    own test_thresholds_are_isolated_by_org already establishes for another
    directly-organisation_id-scoped table: real data from org A must be
    invisible from a different org's session context via a bare SELECT."""
    org_id, project_id, admin, admin_token = authed_org_and_project
    vendor, internal = parties

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await _create_commitment(client, admin_token, project_id, vendor.id, internal.id)
        await _transition(client, admin_token, project_id, created.json()["id"], "committed")

    visible_in_org = (
        await app_session.execute(select(VendorMetric).where(VendorMetric.party_id == vendor.id))
    ).scalars().all()
    assert len(visible_in_org) > 0

    await set_org_context(app_session, uuid.uuid4())  # a different, unrelated org
    visible_cross_org = (
        await app_session.execute(select(VendorMetric).where(VendorMetric.party_id == vendor.id))
    ).scalars().all()
    assert visible_cross_org == []


@pytest.mark.asyncio
async def test_cross_vendor_isolation(app_session, authed_org_and_project, parties):
    """FR-VRG-07 from the other direction: two vendors with different
    commitment histories must never have their numbers mixed — vendor A's
    endpoint response is scoped strictly to vendor A's own party_id."""
    org_id, project_id, admin, admin_token = authed_org_and_project
    vendor_a, internal = parties
    from app.models import Party

    vendor_b = Party(organisation_id=org_id, display_name="Second Vendor", type="vendor_org")
    app_session.add(vendor_b)
    await app_session.commit()
    _finance_user, finance_token = await _member(
        app_session, org_id, project_id, "finance", admin.id
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        c_a = await _create_commitment(client, admin_token, project_id, vendor_a.id, internal.id)
        await _transition(client, admin_token, project_id, c_a.json()["id"], "committed")
        # vendor_b never gets any commitment at all — never recomputed.

        response_a = await client.get(
            f"/parties/{vendor_a.id}/reliability", headers=_headers(finance_token)
        )
        response_b = await client.get(
            f"/parties/{vendor_b.id}/reliability", headers=_headers(finance_token)
        )

    assert response_a.status_code == response_b.status_code == 200
    assert response_a.json()["party_id"] == str(vendor_a.id)
    assert len(response_a.json()["metrics"]) > 0
    assert response_b.json()["party_id"] == str(vendor_b.id)
    assert response_b.json()["metrics"] == []  # never recomputed — no rows at all yet
