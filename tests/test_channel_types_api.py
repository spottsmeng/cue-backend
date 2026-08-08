"""GET /channel-types (app/api/channel_types.py) — read-only discovery of
valid Channel.type / Evidence.channel values, now that they're
channel_types reference-table rows rather than a closed Literal
(app/models/channel_type.py). Also covers the RLS policy on channel_types
itself: platform-shipped rows (organisation_id NULL) visible to every
tenant, a tenant's own future extension row (organisation_id set) visible
only to that tenant — the policy ontology_terms itself is missing (this
session's own migration note, alembic/versions/
c3f6a2b9d417_add_channel_types_reference_table.py).
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.models import ChannelType, Organisation
from main import app
from seed_data.channel_types import CHANNEL_TYPES
from tests.conftest import set_org_context


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_list_channel_types_returns_seeded_platform_rows(authed_org_and_project):
    _org_id, _project_id, _admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/channel-types", headers=_headers(admin_token))

    assert response.status_code == 200, response.text
    codes = {row["code"] for row in response.json()}
    assert codes == {code for code, _capability in CHANNEL_TYPES}
    by_code = {row["code"]: row for row in response.json()}
    assert by_code["whatsapp"]["capability"] == "external_vendor_chat"
    assert by_code["manual"]["capability"] is None
    assert all(row["active"] for row in response.json())


@pytest.mark.asyncio
async def test_list_channel_types_filters_by_capability(authed_org_and_project):
    _org_id, _project_id, _admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/channel-types", params={"capability": "file_storage"}, headers=_headers(admin_token)
        )

    assert response.status_code == 200, response.text
    codes = {row["code"] for row in response.json()}
    assert codes == {"sharepoint", "nextcloud"}


@pytest.mark.asyncio
async def test_tenant_channel_type_extension_is_isolated_by_rls(app_session):
    """The schema hook for the not-yet-built Configuration UI
    (app/models/channel_type.py's own docstring) — a tenant's own future
    channel_types row must be invisible to every other tenant, while every
    platform row (organisation_id NULL) stays visible to all of them."""
    org_a = uuid.uuid4()
    org_b = uuid.uuid4()

    # Each org's own row must be inserted under its own context — the RLS
    # policy's USING doubles as the implicit WITH CHECK for INSERT, so
    # inserting org_b's row while org_a's context is active would violate
    # it (id = current_org_id). Same "chicken-and-egg" note
    # tests/conftest.py's org_and_project fixture already documents.
    await set_org_context(app_session, org_a)
    app_session.add(Organisation(id=org_a, name="Org A"))
    await app_session.flush()

    await set_org_context(app_session, org_b)
    app_session.add(Organisation(id=org_b, name="Org B"))
    await app_session.commit()

    await set_org_context(app_session, org_a)
    app_session.add(
        ChannelType(code=f"acme-crm-{org_a.hex[:8]}", capability="team_collaboration", organisation_id=org_a)
    )
    await app_session.commit()

    await set_org_context(app_session, org_a)
    own_codes = {row.code for row in (await app_session.execute(select(ChannelType))).scalars()}
    assert f"acme-crm-{org_a.hex[:8]}" in own_codes
    assert "whatsapp" in own_codes  # platform row, visible alongside the tenant's own

    await set_org_context(app_session, org_b)
    other_orgs_codes = {row.code for row in (await app_session.execute(select(ChannelType))).scalars()}
    assert f"acme-crm-{org_a.hex[:8]}" not in other_orgs_codes
    assert "whatsapp" in other_orgs_codes  # platform row, still visible
