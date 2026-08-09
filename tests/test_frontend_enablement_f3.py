"""Two small, scoped backend additions made while extending `Prompt F3 —
Foresight, Risks and Deviations.txt` (frontend), same "close the gap you
find, document it" pattern as the five post-M10 additions and F1's own two
additions `backend/PROGRESS.md` already lists:

1. `OntologyTermOut.id` — the response was keyed by `code` alone (a
   deliberate choice for *picking* a term to write), but that left no way
   to resolve an already-persisted `*_term_id` FK (e.g.
   `DeviationOut.class_term_id`) back to a human-readable label. F2 hit the
   identical gap for `MilestoneOut.type_term_id` and worked around it by
   never displaying a milestone's type at all; F3 closes it for real
   instead of adding a second workaround.
2. `GET /projects/{project_id}/members` — nothing let an ordinary write-role
   member look up a fellow member's user id by name; only an org
   administrator could, via `/admin/roles`, and even that response has no
   display_name/email. `DeviationResolveRequest.resolution_owner` (FR-DEV-03)
   needs exactly this to be usable by anyone but an org admin.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.identity.config import get_identity_settings
from app.identity.models import Membership, User
from app.models import Organisation, OntologyTerm, Project
from main import app
from tests.conftest import mint_token, set_org_context


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_ontology_term_out_carries_the_real_term_id(app_session, authed_org_and_project):
    org_id, project_id, _admin, token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/projects/{project_id}/ontology-terms",
            params={"category": "deviation_class"},
            headers=_headers(token),
        )
    assert response.status_code == 200, response.text
    by_code = {row["code"]: row for row in response.json()}
    assert "spec_drift" in by_code

    # Not just "some UUID is present" — the *real* row's id, confirmed by
    # querying the table this response is derived from directly.
    real_term = (
        await app_session.execute(
            select(OntologyTerm).where(
                OntologyTerm.category == "deviation_class", OntologyTerm.code == "spec_drift"
            )
        )
    ).scalar_one()
    assert by_code["spec_drift"]["id"] == str(real_term.id)


@pytest.mark.asyncio
async def test_list_project_members_returns_name_email_and_role(
    app_session, authed_org_and_project
):
    org_id, project_id, admin, admin_token = authed_org_and_project

    # A second member with a real display_name, so this test covers both a
    # null display_name (the admin fixture never sets one) and a real one.
    await set_org_context(app_session, org_id)
    subject = f"finance-{uuid.uuid4()}"
    finance_user = User(
        organisation_id=org_id,
        issuer=get_identity_settings().local_issuer,
        external_subject=subject,
        email=f"{subject}@example.test",
        display_name="Finance Person",
    )
    app_session.add(finance_user)
    await app_session.flush()
    app_session.add(
        Membership(user_id=finance_user.id, project_id=project_id, role="finance", granted_by=admin.id)
    )
    await app_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/projects/{project_id}/members", headers=_headers(admin_token))

    assert response.status_code == 200, response.text
    by_user_id = {row["user_id"]: row for row in response.json()}
    assert len(by_user_id) == 2

    assert by_user_id[str(admin.id)]["role"] == "administrator"
    assert by_user_id[str(admin.id)]["email"] == admin.email
    assert by_user_id[str(admin.id)]["display_name"] is None  # authed_org_and_project never sets one

    assert by_user_id[str(finance_user.id)]["role"] == "finance"
    assert by_user_id[str(finance_user.id)]["display_name"] == "Finance Person"


@pytest.mark.asyncio
async def test_list_project_members_requires_project_membership(app_session, authed_org_and_project):
    """Same "no membership looks identical to a nonexistent project" rule
    `/members/me`'s own test file already establishes for that endpoint."""
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
            f"/projects/{project_id}/members", headers=_headers(outsider_token)
        )
    assert response.status_code == 404  # not a member — same as a nonexistent project


@pytest.mark.asyncio
async def test_list_project_members_does_not_leak_across_projects(
    app_session, authed_org_and_project, seeded_vertical_id
):
    """`memberships` has no `organisation_id` column of its own — RLS scopes
    it via a join to `projects` (same note this table's other tests already
    give). This exercises the app-level WHERE clause independently of RLS:
    a second project, in a *different* organisation, with its own member,
    must never appear in the first project's own member list."""
    org_id, project_id, admin, admin_token = authed_org_and_project

    other_org_id, other_project_id = uuid.uuid4(), uuid.uuid4()
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
    other_subject = f"other-admin-{uuid.uuid4()}"
    other_user = User(
        organisation_id=other_org_id,
        issuer=get_identity_settings().local_issuer,
        external_subject=other_subject,
        email=f"{other_subject}@example.test",
    )
    app_session.add(other_user)
    await app_session.flush()
    app_session.add(
        Membership(
            user_id=other_user.id, project_id=other_project_id, role="administrator", granted_by=other_user.id
        )
    )
    await app_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/projects/{project_id}/members", headers=_headers(admin_token))

    assert response.status_code == 200, response.text
    user_ids = {row["user_id"] for row in response.json()}
    assert user_ids == {str(admin.id)}
    assert str(other_user.id) not in user_ids
