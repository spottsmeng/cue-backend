"""GET /projects/{id}/ontology-terms (app/api/ontology.py) — read-only
discovery of the valid *_code values for one ontology_terms category,
resolved against a project's own effective three-tier vocabulary
(CUE-PRD.md §4.2.1). Reuses app/twin/service.py's list_ontology_terms, the
same resolution get_milestone_type_term already exercises indirectly via
milestone creation — this file proves the discovery endpoint itself, plus
the three-tier override behaviour (a tenant-extension term shadowing the
platform default) and RLS/role-gating as independent properties, per this
project's established testing convention.
"""

import uuid
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.identity.config import get_identity_settings
from app.identity.models import User
from app.models import OntologyTerm
from main import app
from tests.conftest import MILESTONE_TYPES, mint_token, set_org_context


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_list_ontology_terms_returns_the_seeded_milestone_types(authed_org_and_project):
    _org_id, project_id, _admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/projects/{project_id}/ontology-terms",
            params={"category": "milestone_type"},
            headers=_headers(admin_token),
        )

    assert response.status_code == 200, response.text
    codes = {row["code"] for row in response.json()}
    assert codes == {code for code, _en, _zh in MILESTONE_TYPES}
    by_code = {row["code"]: row for row in response.json()}
    assert by_code["doors"]["label_en"] == "Doors"
    assert by_code["doors"]["label_zh"] == "开幕"


@pytest.mark.asyncio
async def test_list_ontology_terms_returns_universal_commitment_acts(authed_org_and_project):
    _org_id, project_id, _admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/projects/{project_id}/ontology-terms",
            params={"category": "commitment_act"},
            headers=_headers(admin_token),
        )

    assert response.status_code == 200, response.text
    codes = {row["code"] for row in response.json()}
    assert "confirm" in codes and "escalate" in codes


@pytest.mark.asyncio
async def test_list_ontology_terms_unknown_category_returns_empty_not_error(authed_org_and_project):
    _org_id, project_id, _admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/projects/{project_id}/ontology-terms",
            params={"category": "not_a_real_category"},
            headers=_headers(admin_token),
        )

    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_list_ontology_terms_tenant_extension_shadows_platform_term(
    app_session, authed_org_and_project
):
    """A tenant's own row for a code that already exists at the vertical-
    pack tier must win — same most-specific-wins resolution
    get_milestone_type_term already relies on for single-code lookups."""
    org_id, project_id, _admin, admin_token = authed_org_and_project

    project = (
        await app_session.execute(
            select(OntologyTerm).where(OntologyTerm.category == "milestone_type", OntologyTerm.code == "doors")
        )
    ).scalar_one()
    await set_org_context(app_session, org_id)
    app_session.add(
        OntologyTerm(
            category="milestone_type",
            code="doors",
            label_en="Doors (tenant override)",
            label_zh="开幕（租户覆盖）",
            vertical_id=project.vertical_id,
            organisation_id=org_id,
            effective_from=datetime.now(timezone.utc),
        )
    )
    await app_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/projects/{project_id}/ontology-terms",
            params={"category": "milestone_type"},
            headers=_headers(admin_token),
        )

    assert response.status_code == 200, response.text
    by_code = {row["code"]: row for row in response.json()}
    assert len(by_code) == len(MILESTONE_TYPES)  # still one row per code, not two
    assert by_code["doors"]["label_en"] == "Doors (tenant override)"


@pytest.mark.asyncio
async def test_ontology_terms_requires_project_membership(app_session, authed_org_and_project):
    org_id, project_id, _admin, _admin_token = authed_org_and_project
    outsider_subject = f"outsider-{uuid.uuid4()}"
    outsider = User(
        organisation_id=org_id,
        issuer=get_identity_settings().local_issuer,
        external_subject=outsider_subject,
        email=f"{outsider_subject}@example.test",
    )
    await set_org_context(app_session, org_id)
    app_session.add(outsider)
    await app_session.commit()
    outsider_token = mint_token(org_id, subject=outsider_subject, email=outsider.email)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/projects/{project_id}/ontology-terms",
            params={"category": "milestone_type"},
            headers=_headers(outsider_token),
        )

    assert response.status_code == 404  # not a member — same as a nonexistent project
