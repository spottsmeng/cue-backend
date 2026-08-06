"""REST endpoints for /projects/{id}/commitments (PRD §11.2): list (filtered
by state/party/due window), read, create, verify, transitions. Through the
real ASGI app, same pattern as test_health.py / test_projects_api.py.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.models import Organisation, Party
from main import app
from tests.conftest import set_org_context


def _headers(org_id, actor_id=None):
    headers = {"X-Org-Id": str(org_id)}
    if actor_id is not None:
        headers["X-User-Id"] = str(actor_id)
    return headers


async def _create_commitment(client, org_id, project_id, vendor_id, internal_id, actor_id=None, **overrides):
    body = {
        "party_id": str(vendor_id),
        "counterparty_id": str(internal_id),
        "act_type": "commit",
        "deliverable_en": "LED screen install",
        **overrides,
    }
    return await client.post(
        f"/projects/{project_id}/commitments", headers=_headers(org_id, actor_id), json=body
    )


@pytest.mark.asyncio
async def test_create_commitment_manual_evidence_and_default_state(org_and_project, parties):
    org_id, project_id = org_and_project
    vendor, internal = parties
    actor_id = uuid.uuid4()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await _create_commitment(
            client, org_id, project_id, vendor.id, internal.id, actor_id=actor_id
        )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["state"] == "proposed"
    assert body["verification_state"] == "auto"
    assert body["confidence"] == 1.0
    # FR-LED-10: a manually created commitment references the user action as
    # its evidence — the DB-layer evidence trigger would have rejected the
    # commit outright if this weren't populated.
    assert len(body["evidence"]) == 1
    assert body["evidence"][0]["channel"] == "manual"
    assert str(actor_id) in body["evidence"][0]["original_text"]


@pytest.mark.asyncio
async def test_create_commitment_with_amount_routes_to_pending_verification(
    org_and_project, parties
):
    """FR-LED-07 applies to manual entry too, not just extraction."""
    org_id, project_id = org_and_project
    vendor, internal = parties

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await _create_commitment(
            client, org_id, project_id, vendor.id, internal.id, amount=8400, currency="SGD"
        )

    assert response.status_code == 201, response.text
    assert response.json()["verification_state"] == "pending_verification"


@pytest.mark.asyncio
async def test_create_commitment_unknown_act_type_is_rejected(org_and_project, parties):
    org_id, project_id = org_and_project
    vendor, internal = parties

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await _create_commitment(
            client, org_id, project_id, vendor.id, internal.id, act_type="not_a_real_act"
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_create_commitment_unknown_party_is_rejected(org_and_project, parties):
    """Gap 1: a party_id that doesn't exist at all must 422, not fall through
    to the DB's FK constraint and surface as a raw 500."""
    org_id, project_id = org_and_project
    vendor, internal = parties
    bogus_party_id = uuid.uuid4()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await _create_commitment(
            client, org_id, project_id, bogus_party_id, internal.id
        )
    assert response.status_code == 422
    assert "party_id" in response.json()["detail"]


@pytest.mark.asyncio
async def test_create_commitment_party_from_another_org_is_rejected(
    app_session, org_and_project, parties
):
    """`parties` has no RLS policy of its own — this is the one test proving
    the app-level organisation_id check is actually load-bearing, not just a
    nicety: a party that's perfectly real, just in a different tenant, must
    still be rejected."""
    org_id, project_id = org_and_project
    vendor, internal = parties

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
        response = await _create_commitment(
            client, org_id, project_id, other_org_party.id, internal.id
        )
    assert response.status_code == 422
    assert "party_id" in response.json()["detail"]


@pytest.mark.asyncio
async def test_create_commitment_unknown_deliverable_is_rejected(org_and_project, parties):
    org_id, project_id = org_and_project
    vendor, internal = parties
    bogus_deliverable_id = uuid.uuid4()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await _create_commitment(
            client,
            org_id,
            project_id,
            vendor.id,
            internal.id,
            deliverable_id=str(bogus_deliverable_id),
        )
    assert response.status_code == 422
    assert "deliverable_id" in response.json()["detail"]


@pytest.mark.asyncio
async def test_list_commitments_filters_by_state(org_and_project, parties):
    org_id, project_id = org_and_project
    vendor, internal = parties

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await _create_commitment(client, org_id, project_id, vendor.id, internal.id)
        commitment_id = create_resp.json()["id"]

        await client.post(
            f"/projects/{project_id}/commitments/{commitment_id}/transitions",
            headers=_headers(org_id),
            json={"to_state": "committed"},
        )

        proposed = await client.get(
            f"/projects/{project_id}/commitments?state=proposed", headers=_headers(org_id)
        )
        committed = await client.get(
            f"/projects/{project_id}/commitments?state=committed", headers=_headers(org_id)
        )

    assert commitment_id not in [c["id"] for c in proposed.json()]
    assert commitment_id in [c["id"] for c in committed.json()]


@pytest.mark.asyncio
async def test_list_commitments_filters_by_party(org_and_project, parties):
    org_id, project_id = org_and_project
    vendor, internal = parties
    other_vendor_id = uuid.uuid4()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await _create_commitment(client, org_id, project_id, vendor.id, internal.id)
        commitment_id = created.json()["id"]

        by_vendor = await client.get(
            f"/projects/{project_id}/commitments?party_id={vendor.id}", headers=_headers(org_id)
        )
        by_other = await client.get(
            f"/projects/{project_id}/commitments?party_id={other_vendor_id}",
            headers=_headers(org_id),
        )

    assert commitment_id in [c["id"] for c in by_vendor.json()]
    assert by_other.json() == []


@pytest.mark.asyncio
async def test_read_commitment_not_found(org_and_project):
    org_id, project_id = org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/projects/{project_id}/commitments/{uuid.uuid4()}", headers=_headers(org_id)
        )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_verify_without_corrections_confirms(org_and_project, parties):
    org_id, project_id = org_and_project
    vendor, internal = parties
    actor_id = uuid.uuid4()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await _create_commitment(client, org_id, project_id, vendor.id, internal.id)
        commitment_id = created.json()["id"]

        response = await client.post(
            f"/projects/{project_id}/commitments/{commitment_id}/verify",
            headers=_headers(org_id, actor_id),
            json={},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["verification_state"] == "human_verified"
    assert body["verified_by"] == str(actor_id)
    assert body["verified_at"] is not None


@pytest.mark.asyncio
async def test_verify_with_corrections_records_correction(org_and_project, parties):
    """FR-LED-08 ('correct if needed') + FR-LED-09 (correction is distinct
    training signal from a plain confirmation)."""
    org_id, project_id = org_and_project
    vendor, internal = parties

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await _create_commitment(
            client, org_id, project_id, vendor.id, internal.id, amount=8400, currency="SGD"
        )
        commitment_id = created.json()["id"]

        response = await client.post(
            f"/projects/{project_id}/commitments/{commitment_id}/verify",
            headers=_headers(org_id),
            json={"corrections": {"amount": 9000}},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["verification_state"] == "human_corrected"
    assert body["amount"] == 9000.0


@pytest.mark.asyncio
async def test_valid_transition_succeeds(org_and_project, parties):
    org_id, project_id = org_and_project
    vendor, internal = parties

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await _create_commitment(client, org_id, project_id, vendor.id, internal.id)
        commitment_id = created.json()["id"]

        response = await client.post(
            f"/projects/{project_id}/commitments/{commitment_id}/transitions",
            headers=_headers(org_id),
            json={"to_state": "committed", "reason": "vendor confirmed"},
        )

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "committed"


@pytest.mark.asyncio
async def test_invalid_transition_is_rejected_with_409(org_and_project, parties):
    """FR-LCY-01 through the API — proposed -> delivered directly is not a
    permitted transition (PRD §6.5's state diagram)."""
    org_id, project_id = org_and_project
    vendor, internal = parties

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await _create_commitment(client, org_id, project_id, vendor.id, internal.id)
        commitment_id = created.json()["id"]

        response = await client.post(
            f"/projects/{project_id}/commitments/{commitment_id}/transitions",
            headers=_headers(org_id),
            json={"to_state": "delivered"},
        )

    assert response.status_code == 409
    assert "not a permitted commitment transition" in response.json()["detail"]


@pytest.mark.asyncio
async def test_commitments_are_isolated_by_org(org_and_project, parties):
    org_id, project_id = org_and_project
    vendor, internal = parties
    other_org_id = uuid.uuid4()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await _create_commitment(client, org_id, project_id, vendor.id, internal.id)
        commitment_id = created.json()["id"]

        # Different org has no visibility into this project at all — RLS
        # denies the parent project lookup, so this 404s rather than 200/403.
        response = await client.get(
            f"/projects/{project_id}/commitments/{commitment_id}", headers=_headers(other_org_id)
        )

    assert response.status_code == 404
