"""GET /parties (app/api/parties.py's list_router) — the vendor/contact
directory /parties/{id}/reliability never had a way to populate: that
endpoint needs a party_id the caller already knows, and until this router
existed nothing in this codebase could tell a caller which party_ids exist.
require_org_finance_or_administrator-gated (widened from require_org_finance
alone during Prompt F7's own gap-audit check — an administrator with neither
finance nor producer needs this directory too, to pick a party for FR-NRM-03's
channel-identity override or FR-NRM-04's organisation-mapping write control),
same org-scoped explicit-filter shape (Party has no RLS policy of its own) as
the reliability endpoints it sits beside — tested here as the same two
independent properties this project's other org-wide surfaces already
establish (role-gating, cross-org isolation).
"""

import uuid
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from app.identity.config import get_identity_settings
from app.identity.models import Membership, User
from app.models import OntologyTerm, Organisation, Party
from main import app
from tests.conftest import mint_token, set_org_context


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _member(app_session, org_id, project_id, role, granted_by):
    """`granted_by` must be a real users.id (memberships.granted_by's FK
    target) — every call site below passes authed_org_and_project's own
    admin.id, never a Party id."""
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


@pytest.mark.asyncio
async def test_list_parties_returns_seeded_org_parties(app_session, authed_org_and_project, parties):
    org_id, project_id, admin, _admin_token = authed_org_and_project
    _finance, finance_token = await _member(app_session, org_id, project_id, "finance", admin.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/parties", headers=_headers(finance_token))

    assert response.status_code == 200, response.text
    names = {row["display_name"] for row in response.json()}
    assert names == {"Test Vendor", "Test Internal"}


@pytest.mark.asyncio
async def test_list_parties_filters_by_type_and_city(app_session, authed_org_and_project, parties):
    org_id, project_id, admin, _admin_token = authed_org_and_project
    vendor, _internal = parties
    vendor.city = "Singapore"
    await app_session.commit()
    _finance, finance_token = await _member(app_session, org_id, project_id, "finance", admin.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/parties", params={"type": "vendor_org", "city": "Singapore"}, headers=_headers(finance_token)
        )

    assert response.status_code == 200, response.text
    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["display_name"] == "Test Vendor"


@pytest.mark.asyncio
async def test_list_parties_filters_by_vendor_category_code(app_session, authed_org_and_project, parties):
    org_id, project_id, admin, _admin_token = authed_org_and_project
    vendor, _internal = parties

    await set_org_context(app_session, org_id)
    term = OntologyTerm(
        category="vendor_category", code="av_production", label_en="AV production", label_zh="AV制作",
        effective_from=datetime.now(timezone.utc),
    )
    app_session.add(term)
    await app_session.flush()
    vendor.vendor_category_term_id = term.id
    await app_session.commit()
    _finance, finance_token = await _member(app_session, org_id, project_id, "finance", admin.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        matching = await client.get(
            "/parties", params={"vendor_category": "av_production"}, headers=_headers(finance_token)
        )
        non_matching = await client.get(
            "/parties", params={"vendor_category": "catering"}, headers=_headers(finance_token)
        )

    assert matching.status_code == 200, matching.text
    assert {row["display_name"] for row in matching.json()} == {"Test Vendor"}
    assert non_matching.json() == []


@pytest.mark.asyncio
async def test_list_parties_requires_finance_or_producer_role(app_session, authed_org_and_project, parties):
    org_id, project_id, admin, _admin_token = authed_org_and_project
    _pm, pm_token = await _member(app_session, org_id, project_id, "project_manager", admin.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/parties", headers=_headers(pm_token))

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_list_parties_allows_administrator_without_finance_or_producer(
    app_session, authed_org_and_project, parties
):
    """Prompt F7's own gap-audit fix: an administrator (Admin console's own
    gate) who holds neither finance nor producer must still be able to
    browse the party directory — they need it to pick a target for
    FR-NRM-03's channel-identity override, an administrator-only action
    that would otherwise have no way to name a party at all."""
    org_id, project_id, admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/parties", headers=_headers(admin_token))

    assert response.status_code == 200, response.text
    assert {row["display_name"] for row in response.json()} >= {"Test Vendor"}


@pytest.mark.asyncio
async def test_read_party_returns_the_seeded_party(app_session, authed_org_and_project, parties):
    """Frontend-enablement addition: GET /parties/{party_id} — a vendor
    detail page's own single-resource read, closed during F6's own
    gap-audit check (frontend/CLAUDE.md's Class A/B checklist)."""
    org_id, project_id, admin, _admin_token = authed_org_and_project
    vendor, _internal = parties
    _finance, finance_token = await _member(app_session, org_id, project_id, "finance", admin.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/parties/{vendor.id}", headers=_headers(finance_token))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == str(vendor.id)
    assert body["display_name"] == "Test Vendor"


@pytest.mark.asyncio
async def test_read_party_404_for_another_organisations_party(app_session, authed_org_and_project, parties):
    org_id, project_id, admin, _admin_token = authed_org_and_project
    _finance, finance_token = await _member(app_session, org_id, project_id, "finance", admin.id)

    org_b = uuid.uuid4()
    await set_org_context(app_session, org_b)
    app_session.add(Organisation(id=org_b, name="Org B"))
    await app_session.flush()
    other_party = Party(organisation_id=org_b, display_name="Org B Vendor", type="vendor_org")
    app_session.add(other_party)
    await app_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/parties/{other_party.id}", headers=_headers(finance_token))

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_read_party_allows_administrator_without_finance_or_producer(
    app_session, authed_org_and_project, parties
):
    org_id, project_id, admin, admin_token = authed_org_and_project
    vendor, _internal = parties

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/parties/{vendor.id}", headers=_headers(admin_token))

    assert response.status_code == 200, response.text
    assert response.json()["id"] == str(vendor.id)


@pytest.mark.asyncio
async def test_read_party_requires_finance_or_producer_role(app_session, authed_org_and_project, parties):
    org_id, project_id, admin, _admin_token = authed_org_and_project
    vendor, _internal = parties
    _pm, pm_token = await _member(app_session, org_id, project_id, "project_manager", admin.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/parties/{vendor.id}", headers=_headers(pm_token))

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_list_parties_is_isolated_across_organisations(app_session, authed_org_and_project, parties):
    """FR-VRG-07's own "never expose a vendor's metrics to another vendor"
    starts here: an org that never granted access to another org's parties
    must not see them in this directory either."""
    org_a, project_a, admin_a, _admin_a_token = authed_org_and_project
    _finance_a, finance_a_token = await _member(app_session, org_a, project_a, "finance", admin_a.id)

    org_b = uuid.uuid4()
    await set_org_context(app_session, org_b)
    app_session.add(Organisation(id=org_b, name="Org B"))
    await app_session.flush()
    app_session.add(Party(organisation_id=org_b, display_name="Org B Vendor", type="vendor_org"))
    await app_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/parties", headers=_headers(finance_a_token))

    assert response.status_code == 200, response.text
    names = {row["display_name"] for row in response.json()}
    assert "Org B Vendor" not in names
