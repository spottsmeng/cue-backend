"""REST endpoints for /admin/consent (PRD §6.14 FR-ADM-07): list, export,
action-request — the consent ledger FR-CAP-06/07's notice/accept/object/
opt-out outcomes are recorded into. Gated by require_org_administrator
(app/api/deps.py), not require_project_role — see that dependency's
docstring for why this is a deliberately different access rule.
"""

import csv
import io
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.models import Party
from main import app


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_non_administrator_is_denied(app_session, authed_org_and_project, parties):
    """require_org_administrator requires the "administrator" role on at
    least one project — a member with no such role anywhere gets 403."""
    org_id, project_id, admin, _admin_token = authed_org_and_project
    vendor, _internal = parties
    from app.identity.config import get_identity_settings
    from app.identity.models import Membership, User
    from tests.conftest import mint_token, set_org_context

    await set_org_context(app_session, org_id)
    subject = f"designer-{uuid.uuid4()}"
    designer = User(
        organisation_id=org_id,
        issuer=get_identity_settings().local_issuer,
        external_subject=subject,
        email=f"{subject}@example.test",
    )
    app_session.add(designer)
    await app_session.flush()
    app_session.add(
        Membership(user_id=designer.id, project_id=project_id, role="designer", granted_by=admin.id)
    )
    await app_session.commit()
    designer_token = mint_token(org_id, subject=subject, email=designer.email)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/admin/consent", headers=_headers(designer_token))
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_action_request_creates_pending_record(authed_org_and_project, parties):
    org_id, project_id, _admin, admin_token = authed_org_and_project
    vendor, _internal = parties

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/admin/consent/action-request",
            headers=_headers(admin_token),
            json={
                "party_id": str(vendor.id),
                "project_id": str(project_id),
                "status": "pending",
                "notice_sent_at": "2026-08-07T09:00:00Z",
            },
        )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "pending"
    assert body["party_id"] == str(vendor.id)


@pytest.mark.asyncio
async def test_consent_status_transitions_are_upserted_not_duplicated(
    app_session, authed_org_and_project, parties
):
    """FR-CAP-07's 'honour opt-out immediately' — a party's consent state is
    one current row per (party, project), not an append-only history: a
    second action-request updates the same row."""
    org_id, project_id, _admin, admin_token = authed_org_and_project
    vendor, _internal = parties

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/admin/consent/action-request",
            headers=_headers(admin_token),
            json={"party_id": str(vendor.id), "project_id": str(project_id), "status": "pending"},
        )
        second = await client.post(
            "/admin/consent/action-request",
            headers=_headers(admin_token),
            json={
                "party_id": str(vendor.id),
                "project_id": str(project_id),
                "status": "accepted",
                "evidence": "Vendor replied 'yes' in WhatsApp group.",
            },
        )
        third = await client.post(
            "/admin/consent/action-request",
            headers=_headers(admin_token),
            json={
                "party_id": str(vendor.id),
                "project_id": str(project_id),
                "status": "opted_out",
                "evidence": "Vendor emailed asking to be removed.",
            },
        )
        listing = await client.get(
            f"/admin/consent?project_id={project_id}&party_id={vendor.id}",
            headers=_headers(admin_token),
        )

    assert first.json()["id"] == second.json()["id"] == third.json()["id"]
    assert third.json()["status"] == "opted_out"
    rows = listing.json()
    assert len(rows) == 1
    assert rows[0]["status"] == "opted_out"


@pytest.mark.asyncio
async def test_action_request_unknown_party_is_rejected(authed_org_and_project):
    org_id, project_id, _admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/admin/consent/action-request",
            headers=_headers(admin_token),
            json={"party_id": str(uuid.uuid4()), "project_id": str(project_id), "status": "pending"},
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_action_request_party_from_another_org_is_rejected(
    app_session, authed_org_and_project
):
    """`parties` has no RLS of its own — same gap
    tests/test_commitments_api.py's own cross-org party test documents."""
    from app.models import Organisation
    from tests.conftest import set_org_context

    org_id, project_id, _admin, admin_token = authed_org_and_project

    other_org_id = uuid.uuid4()
    await set_org_context(app_session, other_org_id)
    app_session.add(Organisation(id=other_org_id, name="Other Org"))
    await app_session.flush()
    other_org_party = Party(
        organisation_id=other_org_id, display_name="Other Org Vendor", type="vendor_org"
    )
    app_session.add(other_org_party)
    await app_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/admin/consent/action-request",
            headers=_headers(admin_token),
            json={
                "party_id": str(other_org_party.id),
                "project_id": str(project_id),
                "status": "pending",
            },
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_list_filters_by_status(authed_org_and_project, parties):
    org_id, project_id, _admin, admin_token = authed_org_and_project
    vendor, internal = parties

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/admin/consent/action-request",
            headers=_headers(admin_token),
            json={"party_id": str(vendor.id), "project_id": str(project_id), "status": "accepted"},
        )
        await client.post(
            "/admin/consent/action-request",
            headers=_headers(admin_token),
            json={"party_id": str(internal.id), "project_id": str(project_id), "status": "objected"},
        )

        accepted = await client.get(
            f"/admin/consent?project_id={project_id}&status=accepted", headers=_headers(admin_token)
        )
        objected = await client.get(
            f"/admin/consent?project_id={project_id}&status=objected", headers=_headers(admin_token)
        )

    assert [r["party_id"] for r in accepted.json()] == [str(vendor.id)]
    assert [r["party_id"] for r in objected.json()] == [str(internal.id)]


@pytest.mark.asyncio
async def test_export_json(authed_org_and_project, parties):
    org_id, project_id, _admin, admin_token = authed_org_and_project
    vendor, _internal = parties

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/admin/consent/action-request",
            headers=_headers(admin_token),
            json={"party_id": str(vendor.id), "project_id": str(project_id), "status": "accepted"},
        )
        response = await client.get(
            f"/admin/consent/export?project_id={project_id}", headers=_headers(admin_token)
        )

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["status"] == "accepted"


@pytest.mark.asyncio
async def test_export_csv(authed_org_and_project, parties):
    org_id, project_id, _admin, admin_token = authed_org_and_project
    vendor, _internal = parties

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/admin/consent/action-request",
            headers=_headers(admin_token),
            json={"party_id": str(vendor.id), "project_id": str(project_id), "status": "accepted"},
        )
        response = await client.get(
            f"/admin/consent/export?project_id={project_id}&format=csv", headers=_headers(admin_token)
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert len(rows) == 1
    assert rows[0]["status"] == "accepted"
    assert rows[0]["party_id"] == str(vendor.id)


@pytest.mark.asyncio
async def test_consent_records_are_isolated_by_org(
    app_session, authed_org_and_project, seeded_vertical_id, parties
):
    """consent_records' RLS policy (project-joined, migration 9ddb100d7e8e)
    is what actually confines /admin/consent to the caller's own tenant —
    exercised here with a second org that genuinely exists and has its own
    Administrator, not just a caller with no administrator role anywhere
    (test_non_administrator_is_denied already covers that simpler case)."""
    from app.identity.config import get_identity_settings
    from app.identity.models import Membership, User
    from tests.conftest import mint_token, set_org_context

    org_id, project_id, _admin, admin_token = authed_org_and_project
    vendor, _internal = parties

    other_org_id = uuid.uuid4()
    other_project_id = uuid.uuid4()
    from app.models import Organisation, Project

    await set_org_context(app_session, other_org_id)
    app_session.add(Organisation(id=other_org_id, name="Other Org"))
    await app_session.flush()
    app_session.add(
        Project(
            id=other_project_id,
            organisation_id=other_org_id,
            vertical_id=seeded_vertical_id,
            name="Other Project",
            timezone="Asia/Singapore",
        )
    )
    await app_session.flush()
    subject = f"other-admin-{uuid.uuid4()}"
    other_admin = User(
        organisation_id=other_org_id,
        issuer=get_identity_settings().local_issuer,
        external_subject=subject,
        email=f"{subject}@example.test",
    )
    app_session.add(other_admin)
    await app_session.flush()
    app_session.add(
        Membership(
            user_id=other_admin.id, project_id=other_project_id, role="administrator",
            granted_by=other_admin.id,
        )
    )
    await app_session.commit()
    other_admin_token = mint_token(other_org_id, subject=subject, email=other_admin.email)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/admin/consent/action-request",
            headers=_headers(admin_token),
            json={"party_id": str(vendor.id), "project_id": str(project_id), "status": "accepted"},
        )
        response = await client.get("/admin/consent", headers=_headers(other_admin_token))

    assert response.status_code == 200
    assert response.json() == []
