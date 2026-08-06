"""FR-ADM-02: "users see only projects they are assigned to." Two
independent properties, tested separately per the build brief rather than as
one, because either could hold while the other is silently broken:

1. RLS's own org-level isolation on `memberships`/`delegations` — proven
   directly against the database, same style as test_rls.py.
2. The *application-level* membership filter (app/identity/service.py's
   accessible_project_ids, used by app/api/projects.py's list_projects and
   app/api/deps.py's require_project_role) — proven through the real API,
   with two users in the *same* organisation (so RLS trivially passes for
   both) where only one is actually a member of the project in question.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.identity.config import get_identity_settings
from app.identity.models import Membership, User
from main import app
from tests.conftest import mint_token, set_org_context


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _fresh_user(app_session, org_id) -> User:
    await set_org_context(app_session, org_id)
    subject = f"user-{uuid.uuid4()}"
    user = User(
        organisation_id=org_id,
        issuer=get_identity_settings().local_issuer,
        external_subject=subject,
        email=f"{subject}@example.test",
    )
    app_session.add(user)
    await app_session.commit()
    return user


# --- Property 1: RLS isolates memberships/delegations by org, independent of
# any application-level filtering. -------------------------------------------


@pytest.mark.asyncio
async def test_memberships_are_rls_isolated_by_org(app_session, authed_org_and_project):
    org_id, project_id, admin, _token = authed_org_and_project

    await set_org_context(app_session, uuid.uuid4())  # a different, unrelated org
    visible = (
        await app_session.execute(select(Membership).where(Membership.project_id == project_id))
    ).scalars().all()
    assert visible == []


@pytest.mark.asyncio
async def test_users_table_is_rls_isolated_by_org(app_session, authed_org_and_project):
    org_id, project_id, admin, _token = authed_org_and_project

    await set_org_context(app_session, uuid.uuid4())  # a different, unrelated org
    visible = (await app_session.execute(select(User).where(User.id == admin.id))).scalar_one_or_none()
    assert visible is None


# --- Property 2: application-level membership scoping, same org, same RLS
# outcome for both users — only the actual membership row differs. -----------


@pytest.mark.asyncio
async def test_list_projects_hides_projects_in_the_same_org_the_user_is_not_a_member_of(
    app_session, authed_org_and_project
):
    org_id, project_id, _admin, admin_token = authed_org_and_project
    outsider = await _fresh_user(app_session, org_id)
    outsider_token = mint_token(org_id, subject=outsider.external_subject, email=outsider.email)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        admin_view = await client.get("/projects", headers=_headers(admin_token))
        outsider_view = await client.get("/projects", headers=_headers(outsider_token))

    assert str(project_id) in [p["id"] for p in admin_view.json()]
    assert str(project_id) not in [p["id"] for p in outsider_view.json()]


@pytest.mark.asyncio
async def test_read_project_hides_it_from_a_same_org_non_member(app_session, authed_org_and_project):
    org_id, project_id, _admin, _admin_token = authed_org_and_project
    outsider = await _fresh_user(app_session, org_id)
    outsider_token = mint_token(org_id, subject=outsider.external_subject, email=outsider.email)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/projects/{project_id}", headers=_headers(outsider_token))
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_list_projects_shows_it_once_membership_is_granted(app_session, authed_org_and_project):
    """The inverse of the above, proving the filter is genuinely membership-
    driven rather than e.g. accidentally permissive for a second reason."""
    org_id, project_id, admin, _admin_token = authed_org_and_project
    newcomer = await _fresh_user(app_session, org_id)
    newcomer_token = mint_token(org_id, subject=newcomer.external_subject, email=newcomer.email)

    await set_org_context(app_session, org_id)
    app_session.add(
        Membership(user_id=newcomer.id, project_id=project_id, role="read_only", granted_by=admin.id)
    )
    await app_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/projects", headers=_headers(newcomer_token))
    assert str(project_id) in [p["id"] for p in response.json()]
