"""FR-NRM-04: app/parties/organisation_mapping.py's effective-dated
person -> vendor-company mapping, plus its /parties/{id}/organisation API
surface."""

from datetime import datetime, timedelta, timezone as dt_timezone

import pytest
from httpx import ASGITransport, AsyncClient

import uuid

from app.identity.config import get_identity_settings
from app.identity.models import Membership, User
from app.models import Party
from app.parties.organisation_mapping import (
    OrganisationMappingError,
    get_current_organisation,
    get_organisation_history,
    set_current_organisation,
)
from main import app
from tests.conftest import mint_token, set_org_context


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _member(app_session, org_id, project_id, role, granted_by):
    """Mirrors tests/test_parties_list_api.py's own helper of the same
    name — a real Membership row, not a stub, so the real require_org_*
    dependency chain is exercised end to end."""
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
async def test_first_mapping_is_current_and_only_history_row(app_session, org_and_project):
    org_id, _project_id = org_and_project
    await set_org_context(app_session, org_id)

    person = Party(organisation_id=org_id, display_name="Ivan Contact", type="person")
    vendor = Party(organisation_id=org_id, display_name="Acme LED Co", type="vendor_org")
    app_session.add_all([person, vendor])
    await app_session.commit()

    mapping = await set_current_organisation(
        app_session, organisation_id=org_id, person_party_id=person.id, organisation_party_id=vendor.id,
        role_title="Account Manager",
    )
    await app_session.commit()

    assert mapping.effective_to is None
    current = await get_current_organisation(app_session, person.id)
    assert current.id == mapping.id

    history = await get_organisation_history(app_session, person.id)
    assert [m.id for m in history] == [mapping.id]


@pytest.mark.asyncio
async def test_moving_vendors_closes_prior_mapping(app_session, org_and_project):
    org_id, _project_id = org_and_project
    await set_org_context(app_session, org_id)

    person = Party(organisation_id=org_id, display_name="Ivan Contact", type="person")
    vendor_a = Party(organisation_id=org_id, display_name="Acme LED Co", type="vendor_org")
    vendor_b = Party(organisation_id=org_id, display_name="Beta Rigging Co", type="vendor_org")
    app_session.add_all([person, vendor_a, vendor_b])
    await app_session.commit()

    t0 = datetime(2026, 1, 1, tzinfo=dt_timezone.utc)
    t1 = datetime(2026, 6, 1, tzinfo=dt_timezone.utc)

    first = await set_current_organisation(
        app_session, organisation_id=org_id, person_party_id=person.id,
        organisation_party_id=vendor_a.id, effective_from=t0,
    )
    await app_session.commit()
    second = await set_current_organisation(
        app_session, organisation_id=org_id, person_party_id=person.id,
        organisation_party_id=vendor_b.id, effective_from=t1,
    )
    await app_session.commit()

    await app_session.refresh(first)
    assert first.effective_to == t1

    # Asking "who did they work for in March 2026" resolves to vendor_a.
    at_march = datetime(2026, 3, 1, tzinfo=dt_timezone.utc)
    historical = await get_current_organisation(app_session, person.id, at=at_march)
    assert historical.organisation_party_id == vendor_a.id

    current = await get_current_organisation(app_session, person.id)
    assert current.id == second.id
    assert current.organisation_party_id == vendor_b.id

    history = await get_organisation_history(app_session, person.id)
    assert [m.organisation_party_id for m in history] == [vendor_a.id, vendor_b.id]


@pytest.mark.asyncio
async def test_reaffirming_same_vendor_is_a_noop(app_session, org_and_project):
    org_id, _project_id = org_and_project
    await set_org_context(app_session, org_id)

    person = Party(organisation_id=org_id, display_name="Ivan Contact", type="person")
    vendor = Party(organisation_id=org_id, display_name="Acme LED Co", type="vendor_org")
    app_session.add_all([person, vendor])
    await app_session.commit()

    first = await set_current_organisation(
        app_session, organisation_id=org_id, person_party_id=person.id, organisation_party_id=vendor.id,
    )
    await app_session.commit()
    second = await set_current_organisation(
        app_session, organisation_id=org_id, person_party_id=person.id, organisation_party_id=vendor.id,
        effective_from=datetime.now(dt_timezone.utc) + timedelta(days=1),
    )
    await app_session.commit()

    assert second.id == first.id
    history = await get_organisation_history(app_session, person.id)
    assert len(history) == 1


@pytest.mark.asyncio
async def test_wrong_party_types_are_rejected(app_session, org_and_project):
    org_id, _project_id = org_and_project
    await set_org_context(app_session, org_id)

    vendor_a = Party(organisation_id=org_id, display_name="Acme LED Co", type="vendor_org")
    vendor_b = Party(organisation_id=org_id, display_name="Beta Rigging Co", type="vendor_org")
    app_session.add_all([vendor_a, vendor_b])
    await app_session.commit()

    with pytest.raises(OrganisationMappingError):
        await set_current_organisation(
            app_session, organisation_id=org_id, person_party_id=vendor_a.id,
            organisation_party_id=vendor_b.id,
        )


@pytest.mark.asyncio
async def test_organisation_api_round_trip(authed_org_and_project):
    org_id, _project_id, _admin, admin_token = authed_org_and_project

    from app.models import Party as PartyModel

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Provision person + vendor through the real app session so RLS sees them.
        from app.core.db import async_session_factory

        async with async_session_factory() as session:
            await set_org_context(session, org_id)
            person = PartyModel(organisation_id=org_id, display_name="Ivan Contact", type="person")
            vendor = PartyModel(organisation_id=org_id, display_name="Acme LED Co", type="vendor_org")
            session.add_all([person, vendor])
            await session.commit()
            person_id, vendor_id = person.id, vendor.id

        create = await client.post(
            f"/parties/{person_id}/organisation",
            headers=_headers(admin_token),
            json={"organisation_party_id": str(vendor_id), "role_title": "PM"},
        )
        assert create.status_code == 201, create.text

        current = await client.get(f"/parties/{person_id}/organisation/current", headers=_headers(admin_token))
        assert current.status_code == 200
        assert current.json()["organisation_party_id"] == str(vendor_id)

        history = await client.get(f"/parties/{person_id}/organisation", headers=_headers(admin_token))
        assert history.status_code == 200
        assert len(history.json()) == 1


@pytest.mark.asyncio
async def test_a_finance_only_user_can_read_organisation_mapping(app_session, authed_org_and_project):
    """Frontend-enablement fix (F6's own gap-audit check): the two GET
    operations are require_org_finance_or_administrator-gated now, not
    require_org_administrator alone — a Finance/Producer user (who can
    already see everything else on a vendor's detail page) must not 403
    here specifically. The write stays admin-only, asserted separately
    below."""
    org_id, project_id, admin, admin_token = authed_org_and_project
    _finance, finance_token = await _member(app_session, org_id, project_id, "finance", admin.id)

    from app.core.db import async_session_factory

    async with async_session_factory() as session:
        await set_org_context(session, org_id)
        person = Party(organisation_id=org_id, display_name="Finance-readable Contact", type="person")
        vendor = Party(organisation_id=org_id, display_name="Finance-readable Vendor", type="vendor_org")
        session.add_all([person, vendor])
        await session.commit()
        person_id, vendor_id = person.id, vendor.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # The write is still administrator-only — a finance-only user 403s here.
        create = await client.post(
            f"/parties/{person_id}/organisation",
            headers=_headers(finance_token),
            json={"organisation_party_id": str(vendor_id)},
        )
        assert create.status_code == 403, create.text

        # Set the mapping as admin, then confirm finance can read both GETs.
        create_as_admin = await client.post(
            f"/parties/{person_id}/organisation",
            headers=_headers(admin_token),
            json={"organisation_party_id": str(vendor_id)},
        )
        assert create_as_admin.status_code == 201, create_as_admin.text

        current = await client.get(
            f"/parties/{person_id}/organisation/current", headers=_headers(finance_token)
        )
        assert current.status_code == 200, current.text
        assert current.json()["organisation_party_id"] == str(vendor_id)

        history = await client.get(
            f"/parties/{person_id}/organisation", headers=_headers(finance_token)
        )
        assert history.status_code == 200, history.text
        assert len(history.json()) == 1


@pytest.mark.asyncio
async def test_a_project_manager_only_user_is_still_refused_read_access(
    app_session, authed_org_and_project
):
    """Neither finance/producer nor administrator — still 403, same as
    before this fix. The gate was loosened to include Finance/Producer, not
    opened to every role."""
    org_id, project_id, admin, _admin_token = authed_org_and_project
    _pm, pm_token = await _member(app_session, org_id, project_id, "project_manager", admin.id)

    from app.core.db import async_session_factory

    async with async_session_factory() as session:
        await set_org_context(session, org_id)
        person = Party(organisation_id=org_id, display_name="Still-Gated Contact", type="person")
        session.add(person)
        await session.commit()
        person_id = person.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        history = await client.get(f"/parties/{person_id}/organisation", headers=_headers(pm_token))
        assert history.status_code == 403, history.text

        current = await client.get(
            f"/parties/{person_id}/organisation/current", headers=_headers(pm_token)
        )
        assert current.status_code == 403, current.text
