"""REST endpoints for /projects/{id}/ask (PRD §11.2, CUE-PRD.md §6.11
FR-ASK): query · summarise · successor-brief. Through the real ASGI app,
same pattern as tests/test_documents_api.py / tests/test_reports_api.py.

The `query` endpoint's positive "grounded answer" path needs a real
reasoning model and isn't exercised here (tests/test_ask_answer.py covers it
with fakes, injected below the HTTP layer) — what's testable through the
real endpoint without any live model dependency is the "no source" refusal
path (retrieval finds nothing, so the reasoning model is never even
reached), which is exactly FR-ASK-02's structural guarantee this session
cares most about proving end to end.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.identity.config import get_identity_settings
from app.identity.models import Membership, User
from main import app
from tests.conftest import mint_token, set_org_context


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _member(app_session, org_id, project_id, role, granted_by):
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
async def test_query_with_no_matching_source_returns_typed_no_source_variant(authed_org_and_project):
    """FR-ASK-02: asserts the structural fields directly, not that the
    string happens not to mention a source."""
    org_id, project_id, user, token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/ask/query",
            headers=_headers(token),
            json={"question": "what did the vendor say about the catering budget?"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["available"] is False
    assert body["answer"] is None
    assert body["citations"] == []
    assert body["unavailable_reason"]
    assert body["refusal_kind"] == "no_citable_source"
    assert uuid.UUID(body["conversation_id"])


@pytest.mark.asyncio
async def test_query_unknown_conversation_id_is_404(authed_org_and_project):
    org_id, project_id, user, token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/ask/query",
            headers=_headers(token),
            json={"question": "anything", "conversation_id": str(uuid.uuid4())},
        )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_summarise_project_status(authed_org_and_project):
    org_id, project_id, user, token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/ask/summarise",
            headers=_headers(token),
            json={"variant": "project_status"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["variant"] == "project_status"
    assert body["project_status"] is not None
    assert body["vendor_status"] is None


@pytest.mark.asyncio
async def test_summarise_period_digest_requires_period_bounds(authed_org_and_project):
    org_id, project_id, user, token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/ask/summarise",
            headers=_headers(token),
            json={"variant": "period_digest"},
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_successor_brief(authed_org_and_project):
    org_id, project_id, user, token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/ask/successor-brief", headers=_headers(token),
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["project_id"] == str(project_id)
    for section in (
        "open_commitments", "decision_history", "risks", "key_documents",
        "vendor_contacts", "deviations_and_resolutions",
    ):
        assert section in body


@pytest.mark.asyncio
async def test_read_only_member_can_use_ask(app_session, authed_org_and_project):
    """§12.5: 'Ask (all internal users)' — unlike write-gated endpoints
    elsewhere, no WRITE_ROLES/ADMIN_ROLES restriction applies here; a
    read_only member can query/summarise/get a brief the same as anyone
    else with project membership."""
    org_id, project_id, admin, _admin_token = authed_org_and_project
    _viewer, viewer_token = await _member(app_session, org_id, project_id, "read_only", admin.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/ask/summarise",
            headers=_headers(viewer_token),
            json={"variant": "decision_history"},
        )

    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_non_member_gets_404_not_403(authed_org_and_project):
    """RLS + role-gating as two independent properties: a bearer token for a
    real user in the same org, but with no membership on this project at
    all, is 404'd — never leaked as "exists but forbidden," same posture
    require_project_role() documents for every other domain's endpoints."""
    org_id, project_id, _admin, _admin_token = authed_org_and_project
    outsider_token = mint_token(org_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/ask/successor-brief", headers=_headers(outsider_token),
        )

    assert response.status_code == 404
