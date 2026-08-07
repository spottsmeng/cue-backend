"""REST endpoints for /projects/{id}/commitments (PRD §11.2): list (filtered
by state/party/due window), read, create, verify, transitions. Through the
real ASGI app, same pattern as test_health.py / test_projects_api.py —
authenticated via tests/conftest.py's authed_org_and_project fixture (a real
`users` row with "administrator" membership on the project, and a bearer
token for them), not the old X-Org-Id/X-User-Id header stub.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.identity.config import get_identity_settings
from app.identity.models import Membership, User
from app.models import Organisation, Party
from main import app
from tests.conftest import auth_headers, mint_token, set_org_context


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


async def _create_commitment(client, token, project_id, vendor_id, internal_id, **overrides):
    body = {
        "party_id": str(vendor_id),
        "counterparty_id": str(internal_id),
        "act_type": "commit",
        "deliverable_en": "LED screen install",
        **overrides,
    }
    return await client.post(
        f"/projects/{project_id}/commitments", headers=_headers(token), json=body
    )


@pytest.mark.asyncio
async def test_create_commitment_manual_evidence_and_default_state(authed_org_and_project, parties):
    org_id, project_id, user, token = authed_org_and_project
    vendor, internal = parties

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await _create_commitment(client, token, project_id, vendor.id, internal.id)

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
    assert str(user.id) in body["evidence"][0]["original_text"]


@pytest.mark.asyncio
async def test_create_commitment_with_amount_routes_to_pending_verification(
    authed_org_and_project, parties
):
    """FR-LED-07 applies to manual entry too, not just extraction."""
    org_id, project_id, user, token = authed_org_and_project
    vendor, internal = parties

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await _create_commitment(
            client, token, project_id, vendor.id, internal.id, amount=8400, currency="SGD"
        )

    assert response.status_code == 201, response.text
    assert response.json()["verification_state"] == "pending_verification"


@pytest.mark.asyncio
async def test_create_commitment_unknown_act_type_is_rejected(authed_org_and_project, parties):
    org_id, project_id, user, token = authed_org_and_project
    vendor, internal = parties

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await _create_commitment(
            client, token, project_id, vendor.id, internal.id, act_type="not_a_real_act"
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_create_commitment_unknown_party_is_rejected(authed_org_and_project, parties):
    """Gap 1: a party_id that doesn't exist at all must 422, not fall through
    to the DB's FK constraint and surface as a raw 500."""
    org_id, project_id, user, token = authed_org_and_project
    vendor, internal = parties
    bogus_party_id = uuid.uuid4()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await _create_commitment(client, token, project_id, bogus_party_id, internal.id)
    assert response.status_code == 422
    assert "party_id" in response.json()["detail"]


@pytest.mark.asyncio
async def test_create_commitment_party_from_another_org_is_rejected(
    app_session, authed_org_and_project, parties
):
    """`parties` has no RLS policy of its own — this is the one test proving
    the app-level organisation_id check is actually load-bearing, not just a
    nicety: a party that's perfectly real, just in a different tenant, must
    still be rejected."""
    org_id, project_id, user, token = authed_org_and_project
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
            client, token, project_id, other_org_party.id, internal.id
        )
    assert response.status_code == 422
    assert "party_id" in response.json()["detail"]


@pytest.mark.asyncio
async def test_create_commitment_unknown_deliverable_is_rejected(authed_org_and_project, parties):
    org_id, project_id, user, token = authed_org_and_project
    vendor, internal = parties
    bogus_deliverable_id = uuid.uuid4()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await _create_commitment(
            client,
            token,
            project_id,
            vendor.id,
            internal.id,
            deliverable_id=str(bogus_deliverable_id),
        )
    assert response.status_code == 422
    assert "deliverable_id" in response.json()["detail"]


@pytest.mark.asyncio
async def test_list_commitments_filters_by_state(authed_org_and_project, parties):
    org_id, project_id, user, token = authed_org_and_project
    vendor, internal = parties

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await _create_commitment(client, token, project_id, vendor.id, internal.id)
        commitment_id = create_resp.json()["id"]

        await client.post(
            f"/projects/{project_id}/commitments/{commitment_id}/transitions",
            headers=_headers(token),
            json={"to_state": "committed"},
        )

        proposed = await client.get(
            f"/projects/{project_id}/commitments?state=proposed", headers=_headers(token)
        )
        committed = await client.get(
            f"/projects/{project_id}/commitments?state=committed", headers=_headers(token)
        )

    assert commitment_id not in [c["id"] for c in proposed.json()]
    assert commitment_id in [c["id"] for c in committed.json()]


@pytest.mark.asyncio
async def test_list_commitments_filters_by_party(authed_org_and_project, parties):
    org_id, project_id, user, token = authed_org_and_project
    vendor, internal = parties
    other_vendor_id = uuid.uuid4()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await _create_commitment(client, token, project_id, vendor.id, internal.id)
        commitment_id = created.json()["id"]

        by_vendor = await client.get(
            f"/projects/{project_id}/commitments?party_id={vendor.id}", headers=_headers(token)
        )
        by_other = await client.get(
            f"/projects/{project_id}/commitments?party_id={other_vendor_id}",
            headers=_headers(token),
        )

    assert commitment_id in [c["id"] for c in by_vendor.json()]
    assert by_other.json() == []


@pytest.mark.asyncio
async def test_read_commitment_not_found(authed_org_and_project):
    org_id, project_id, user, token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/projects/{project_id}/commitments/{uuid.uuid4()}", headers=_headers(token)
        )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_verify_without_corrections_confirms(authed_org_and_project, parties):
    org_id, project_id, user, token = authed_org_and_project
    vendor, internal = parties

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await _create_commitment(client, token, project_id, vendor.id, internal.id)
        commitment_id = created.json()["id"]

        response = await client.post(
            f"/projects/{project_id}/commitments/{commitment_id}/verify",
            headers=_headers(token),
            json={},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["verification_state"] == "human_verified"
    assert body["verified_by"] == str(user.id)
    assert body["verified_at"] is not None


@pytest.mark.asyncio
async def test_verify_with_corrections_records_correction(authed_org_and_project, parties):
    """FR-LED-08 ('correct if needed') + FR-LED-09 (correction is distinct
    training signal from a plain confirmation)."""
    org_id, project_id, user, token = authed_org_and_project
    vendor, internal = parties

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await _create_commitment(
            client, token, project_id, vendor.id, internal.id, amount=8400, currency="SGD"
        )
        commitment_id = created.json()["id"]

        response = await client.post(
            f"/projects/{project_id}/commitments/{commitment_id}/verify",
            headers=_headers(token),
            json={"corrections": {"amount": 9000}},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["verification_state"] == "human_corrected"
    assert body["amount"] == 9000.0


@pytest.mark.asyncio
async def test_valid_transition_succeeds(authed_org_and_project, parties):
    org_id, project_id, user, token = authed_org_and_project
    vendor, internal = parties

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await _create_commitment(client, token, project_id, vendor.id, internal.id)
        commitment_id = created.json()["id"]

        response = await client.post(
            f"/projects/{project_id}/commitments/{commitment_id}/transitions",
            headers=_headers(token),
            json={"to_state": "committed", "reason": "vendor confirmed"},
        )

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "committed"


@pytest.mark.asyncio
async def test_invalid_transition_is_rejected_with_409(authed_org_and_project, parties):
    """FR-LCY-01 through the API — proposed -> delivered directly is not a
    permitted transition (PRD §6.5's state diagram)."""
    org_id, project_id, user, token = authed_org_and_project
    vendor, internal = parties

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await _create_commitment(client, token, project_id, vendor.id, internal.id)
        commitment_id = created.json()["id"]

        response = await client.post(
            f"/projects/{project_id}/commitments/{commitment_id}/transitions",
            headers=_headers(token),
            json={"to_state": "delivered"},
        )

    assert response.status_code == 409
    assert "not a permitted commitment transition" in response.json()["detail"]


@pytest.mark.asyncio
async def test_commitments_are_isolated_by_org(app_session, authed_org_and_project, parties):
    org_id, project_id, user, token = authed_org_and_project
    vendor, internal = parties

    other_org_id = uuid.uuid4()
    await set_org_context(app_session, other_org_id)
    app_session.add(Organisation(id=other_org_id, name="Other Org"))
    await app_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await _create_commitment(client, token, project_id, vendor.id, internal.id)
        commitment_id = created.json()["id"]

        # A different org entirely has no visibility into this project at
        # all — RLS denies the parent project lookup, so this 404s rather
        # than 200/403.
        response = await client.get(
            f"/projects/{project_id}/commitments/{commitment_id}",
            headers=auth_headers(other_org_id),
        )

    assert response.status_code == 404


# --- payment-status (FR-LED-13, §6.14 FR-ADM-11's shared role gate) ------


@pytest.mark.asyncio
async def test_finance_role_can_set_payment_status(app_session, authed_org_and_project, parties):
    org_id, project_id, admin, admin_token = authed_org_and_project
    vendor, internal = parties
    _finance, finance_token = await _member(app_session, org_id, project_id, "finance", admin.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await _create_commitment(client, admin_token, project_id, vendor.id, internal.id)
        commitment_id = created.json()["id"]

        response = await client.patch(
            f"/projects/{project_id}/commitments/{commitment_id}/payment-status",
            headers=_headers(finance_token),
            json={"payment_status": "invoiced"},
        )

    assert response.status_code == 200, response.text
    assert response.json()["payment_status"] == "invoiced"


@pytest.mark.asyncio
async def test_producer_role_can_set_payment_status(app_session, authed_org_and_project, parties):
    """FINANCE_ROLES = {"finance", "producer"} — Producer is the other half
    of FR-ADM-11's merged Finance & Procurement persona."""
    org_id, project_id, admin, admin_token = authed_org_and_project
    vendor, internal = parties
    _producer, producer_token = await _member(app_session, org_id, project_id, "producer", admin.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await _create_commitment(client, admin_token, project_id, vendor.id, internal.id)
        commitment_id = created.json()["id"]

        response = await client.patch(
            f"/projects/{project_id}/commitments/{commitment_id}/payment-status",
            headers=_headers(producer_token),
            json={"payment_status": "paid"},
        )
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_administrator_cannot_set_payment_status(authed_org_and_project, parties):
    """FR-LED-13: 'settable only by the Finance & Procurement role' —
    Administrator is not one of them, same as Budget's own gate."""
    org_id, project_id, admin, admin_token = authed_org_and_project
    vendor, internal = parties

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await _create_commitment(client, admin_token, project_id, vendor.id, internal.id)
        commitment_id = created.json()["id"]

        response = await client.patch(
            f"/projects/{project_id}/commitments/{commitment_id}/payment-status",
            headers=_headers(admin_token),
            json={"payment_status": "paid"},
        )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_payment_status_is_not_settable_via_verify_endpoint(authed_org_and_project, parties):
    """FR-LED-13: 'never inferred from message content' — structurally
    distinct from the /verify correction path, which handles model-extracted
    fields. CommitmentCorrection has no payment_status field at all, so
    attempting to smuggle it through there is silently ignored by pydantic,
    not applied."""
    org_id, project_id, admin, admin_token = authed_org_and_project
    vendor, internal = parties

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await _create_commitment(client, admin_token, project_id, vendor.id, internal.id)
        commitment_id = created.json()["id"]

        response = await client.post(
            f"/projects/{project_id}/commitments/{commitment_id}/verify",
            headers=_headers(admin_token),
            json={"corrections": {"payment_status": "paid"}},
        )

    assert response.status_code == 200, response.text
    assert response.json()["payment_status"] is None
