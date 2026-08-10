"""REST endpoints for /projects/{id}/budget (PRD §4.3, §6.14 FR-ADM-11):
create baseline, read current, revise. Through the real ASGI app, same
pattern as tests/test_commitments_api.py — Finance-role-gated writes,
mirroring tests/test_rbac.py's allow/deny shape exactly.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.identity.config import get_identity_settings
from app.identity.models import Membership, User
from app.models import Budget
from main import app
from tests.conftest import mint_token, set_org_context


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


def _budget_body(**overrides):
    return {"approved_amount": 50000, "currency": "SGD", **overrides}


@pytest.mark.asyncio
async def test_administrator_cannot_create_budget(authed_org_and_project):
    """FR-ADM-11: 'editable only by the Finance or Producer role' — an
    Administrator (who can do almost everything else on a project) is not
    one of them."""
    org_id, project_id, _admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/budget", headers=_headers(admin_token), json=_budget_body()
        )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_finance_role_can_create_budget_baseline(app_session, authed_org_and_project):
    org_id, project_id, admin, _admin_token = authed_org_and_project
    _finance, finance_token = await _member(app_session, org_id, project_id, "finance", admin.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/budget", headers=_headers(finance_token), json=_budget_body()
        )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["approved_amount"] == 50000.0
    assert body["currency"] == "SGD"
    assert body["is_current"] is True
    assert body["revision_of"] is None


@pytest.mark.asyncio
async def test_producer_role_can_create_budget_baseline(app_session, authed_org_and_project):
    """FR-ADM-11's role gate is Finance-or-Producer, not Finance alone —
    FINANCE_ROLES = {"finance", "producer"} (app/identity/service.py)."""
    org_id, project_id, admin, _admin_token = authed_org_and_project
    _producer, producer_token = await _member(app_session, org_id, project_id, "producer", admin.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/budget", headers=_headers(producer_token), json=_budget_body()
        )
    assert response.status_code == 201, response.text


@pytest.mark.asyncio
async def test_read_current_budget_404s_when_none_exists(authed_org_and_project):
    org_id, project_id, _admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/projects/{project_id}/budget", headers=_headers(admin_token))
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_read_only_member_can_read_but_not_write_budget(app_session, authed_org_and_project):
    """The role that exists specifically to grant visibility without write
    access (same property tests/test_rbac.py's own read_only test asserts
    for commitments) must still be able to see the budget baseline."""
    org_id, project_id, admin, _admin_token = authed_org_and_project
    _finance, finance_token = await _member(app_session, org_id, project_id, "finance", admin.id)
    _viewer, viewer_token = await _member(app_session, org_id, project_id, "read_only", admin.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            f"/projects/{project_id}/budget", headers=_headers(finance_token), json=_budget_body()
        )
        read = await client.get(f"/projects/{project_id}/budget", headers=_headers(viewer_token))
        write = await client.post(
            f"/projects/{project_id}/budget", headers=_headers(viewer_token), json=_budget_body()
        )

    assert read.status_code == 200
    assert write.status_code == 403


@pytest.mark.asyncio
async def test_create_budget_baseline_conflicts_when_one_already_exists(
    app_session, authed_org_and_project
):
    org_id, project_id, admin, _admin_token = authed_org_and_project
    _finance, finance_token = await _member(app_session, org_id, project_id, "finance", admin.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            f"/projects/{project_id}/budget", headers=_headers(finance_token), json=_budget_body()
        )
        second = await client.post(
            f"/projects/{project_id}/budget", headers=_headers(finance_token), json=_budget_body()
        )

    assert first.status_code == 201
    assert second.status_code == 409


@pytest.mark.asyncio
async def test_revise_without_existing_baseline_404s(app_session, authed_org_and_project):
    org_id, project_id, admin, _admin_token = authed_org_and_project
    _finance, finance_token = await _member(app_session, org_id, project_id, "finance", admin.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/budget/revise",
            headers=_headers(finance_token),
            json=_budget_body(),
        )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_revise_creates_new_row_and_never_mutates_the_old_one(
    app_session, authed_org_and_project
):
    """FR-ADM-11: 'full audit trail and revision history on scope-approved
    changes' — Budget's own docstring: 'revision_of/is_current... full
    revision history, never an in-place edit'."""
    org_id, project_id, admin, _admin_token = authed_org_and_project
    _finance, finance_token = await _member(app_session, org_id, project_id, "finance", admin.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        baseline = await client.post(
            f"/projects/{project_id}/budget",
            headers=_headers(finance_token),
            json=_budget_body(approved_amount=50000),
        )
        baseline_id = baseline.json()["id"]

        revision = await client.post(
            f"/projects/{project_id}/budget/revise",
            headers=_headers(finance_token),
            json=_budget_body(approved_amount=76624),
        )
        current = await client.get(f"/projects/{project_id}/budget", headers=_headers(finance_token))

    assert revision.status_code == 201, revision.text
    revision_body = revision.json()
    assert revision_body["id"] != baseline_id
    assert revision_body["revision_of"] == baseline_id
    assert revision_body["approved_amount"] == 76624.0
    assert revision_body["is_current"] is True

    assert current.json()["id"] == revision_body["id"]

    # The prior baseline row still exists, untouched except is_current — a
    # revision is a new row, never an UPDATE of the old row's own amount.
    await set_org_context(app_session, org_id)
    old = (
        await app_session.execute(select(Budget).where(Budget.id == uuid.UUID(baseline_id)))
    ).scalar_one()
    assert old.is_current is False
    assert float(old.approved_amount) == 50000.0


@pytest.mark.asyncio
async def test_budget_history_lists_baseline_and_revision_most_recent_first(
    app_session, authed_org_and_project
):
    """Frontend-enablement addition (Prompt F7's own gap-audit check) —
    GET .../budget only ever returned the current row; nothing showed a
    baseline alongside a revision that superseded it. `revision_of` links
    the two, most-recent-first."""
    org_id, project_id, admin, _admin_token = authed_org_and_project
    _finance, finance_token = await _member(app_session, org_id, project_id, "finance", admin.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        baseline = await client.post(
            f"/projects/{project_id}/budget",
            headers=_headers(finance_token),
            json=_budget_body(approved_amount=50000),
        )
        baseline_id = baseline.json()["id"]
        revision = await client.post(
            f"/projects/{project_id}/budget/revise",
            headers=_headers(finance_token),
            json=_budget_body(approved_amount=76624),
        )
        revision_id = revision.json()["id"]

        history = await client.get(
            f"/projects/{project_id}/budget/history", headers=_headers(finance_token)
        )

    assert history.status_code == 200, history.text
    rows = history.json()
    assert [r["id"] for r in rows] == [revision_id, baseline_id]
    assert rows[0]["is_current"] is True
    assert rows[0]["revision_of"] == baseline_id
    assert rows[1]["is_current"] is False


@pytest.mark.asyncio
async def test_budget_history_empty_list_when_no_baseline_exists(authed_org_and_project):
    org_id, project_id, _admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/projects/{project_id}/budget/history", headers=_headers(admin_token)
        )
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_budget_baseline_has_manual_evidence(app_session, authed_org_and_project):
    """PRD §4.3: Budget's evidence is NOT NULL — 'an approval message...
    otherwise the approving user's action itself, per the same rule as
    FR-LED-10'. The evidence-required DB trigger (migration a895ae03ec5c)
    would have rejected the commit outright if this weren't populated."""
    org_id, project_id, admin, _admin_token = authed_org_and_project
    _finance, finance_token = await _member(app_session, org_id, project_id, "finance", admin.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/budget", headers=_headers(finance_token), json=_budget_body()
        )
    assert response.status_code == 201, response.text
