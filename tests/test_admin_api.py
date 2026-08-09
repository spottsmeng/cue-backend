"""REST endpoints for §11.2's org-wide admin surfaces: /admin/users,
/admin/roles, /admin/delegations, /admin/export — completing the
project-scoped versions app/api/projects.py already built. Gated by
require_org_administrator (app/api/deps.py), a deliberately different
access rule from require_project_role: see that dependency's docstring.
"""

import csv
import io
import uuid
import zipfile

import pytest
from httpx import ASGITransport, AsyncClient

from app.identity.config import get_identity_settings
from app.identity.models import Membership, User
from app.llm.models import LLMUsageEvent
from app.models import Organisation, Project
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


@pytest.mark.asyncio
async def test_list_org_users(app_session, authed_org_and_project):
    org_id, project_id, admin, admin_token = authed_org_and_project
    _finance, _finance_token = await _member(app_session, org_id, project_id, "finance", admin.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/admin/users", headers=_headers(admin_token))

    assert response.status_code == 200
    emails = {u["email"] for u in response.json()}
    assert admin.email in emails
    assert _finance.email in emails


@pytest.mark.asyncio
async def test_read_org_user_not_found_404s(authed_org_and_project):
    org_id, project_id, _admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/admin/users/{uuid.uuid4()}", headers=_headers(admin_token))
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_list_org_roles(app_session, authed_org_and_project):
    org_id, project_id, admin, admin_token = authed_org_and_project
    _finance, _finance_token = await _member(app_session, org_id, project_id, "finance", admin.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        all_roles = await client.get("/admin/roles", headers=_headers(admin_token))
        finance_only = await client.get("/admin/roles?role=finance", headers=_headers(admin_token))

    assert len(all_roles.json()) == 2  # the fixture's own administrator + the finance member
    assert len(finance_only.json()) == 1
    assert finance_only.json()[0]["role"] == "finance"


@pytest.mark.asyncio
async def test_list_org_delegations(app_session, authed_org_and_project):
    org_id, project_id, admin, admin_token = authed_org_and_project
    _delegate, _delegate_token = await _member(app_session, org_id, project_id, "designer", admin.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            f"/projects/{project_id}/delegations",
            headers=_headers(admin_token),
            json={
                "delegate_email": _delegate.email,
                "role": "designer",
                "expires_at": "2099-01-01T00:00:00Z",
            },
        )
        response = await client.get("/admin/delegations", headers=_headers(admin_token))

    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["delegate_id"] == str(_delegate.id)


@pytest.mark.asyncio
async def test_org_admin_visibility_is_distinct_from_project_membership(app_session, seeded_vertical_id):
    """The exact property Prompt 5's testing expectation names explicitly: a
    user who is Administrator on project A but not a member of project B
    must see B via /admin but not via /projects — a genuinely separate
    property from require_project_role's membership check, not one that
    happens to imply the other."""
    org_id = uuid.uuid4()
    project_a_id = uuid.uuid4()
    project_b_id = uuid.uuid4()

    await set_org_context(app_session, org_id)
    app_session.add(Organisation(id=org_id, name="Test Org"))
    await app_session.flush()
    app_session.add(
        Project(
            id=project_a_id, organisation_id=org_id, vertical_id=seeded_vertical_id,
            name="Project A", timezone="Asia/Singapore",
        )
    )
    app_session.add(
        Project(
            id=project_b_id, organisation_id=org_id, vertical_id=seeded_vertical_id,
            name="Project B", timezone="Asia/Singapore",
        )
    )
    await app_session.flush()

    subject = f"admin-a-{uuid.uuid4()}"
    admin_user = User(
        organisation_id=org_id,
        issuer=get_identity_settings().local_issuer,
        external_subject=subject,
        email=f"{subject}@example.test",
    )
    app_session.add(admin_user)
    await app_session.flush()
    # Administrator on project A only — never a member of project B at all.
    app_session.add(
        Membership(
            user_id=admin_user.id, project_id=project_a_id, role="administrator",
            granted_by=admin_user.id,
        )
    )
    await app_session.commit()
    token = mint_token(org_id, subject=subject, email=admin_user.email)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        via_projects = await client.get(f"/projects/{project_b_id}", headers=_headers(token))
        via_admin_export = await client.get(
            f"/admin/export/{project_b_id}", headers=_headers(token)
        )

    # require_project_role: no membership/delegation on B at all -> 404,
    # indistinguishable from a nonexistent project (FR-ADM-02).
    assert via_projects.status_code == 404
    # require_org_administrator: Administrator on A is enough to reach any
    # project in the same org via the admin surface, including B.
    assert via_admin_export.status_code == 200
    assert via_admin_export.json()["project"][0]["id"] == str(project_b_id)


@pytest.mark.asyncio
async def test_non_administrator_denied_from_admin_surfaces(app_session, authed_org_and_project):
    org_id, project_id, admin, _admin_token = authed_org_and_project
    _designer, designer_token = await _member(app_session, org_id, project_id, "designer", admin.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        users = await client.get("/admin/users", headers=_headers(designer_token))
        roles = await client.get("/admin/roles", headers=_headers(designer_token))
        delegations = await client.get("/admin/delegations", headers=_headers(designer_token))
        export = await client.get(f"/admin/export/{project_id}", headers=_headers(designer_token))

    assert users.status_code == 403
    assert roles.status_code == 403
    assert delegations.status_code == 403
    assert export.status_code == 403


@pytest.mark.asyncio
async def test_export_project_json_bundle(authed_org_and_project, parties):
    org_id, project_id, admin, admin_token = authed_org_and_project
    vendor, internal = parties

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            f"/projects/{project_id}/commitments",
            headers=_headers(admin_token),
            json={
                "party_id": str(vendor.id),
                "counterparty_id": str(internal.id),
                "act_type": "commit",
                "deliverable_en": "LED screen install",
            },
        )
        response = await client.get(f"/admin/export/{project_id}", headers=_headers(admin_token))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["project"][0]["id"] == str(project_id)
    assert len(body["commitments"]) == 1
    assert body["commitments"][0]["deliverable_en"] == "LED screen install"
    assert len(body["evidence"]) == 1
    assert len(body["audit_log"]) == 1  # the "created" action from commitment creation
    assert body["budgets"] == []


@pytest.mark.asyncio
async def test_export_project_csv_bundle(authed_org_and_project, parties):
    org_id, project_id, admin, admin_token = authed_org_and_project
    vendor, internal = parties

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            f"/projects/{project_id}/commitments",
            headers=_headers(admin_token),
            json={
                "party_id": str(vendor.id),
                "counterparty_id": str(internal.id),
                "act_type": "commit",
                "deliverable_en": "LED screen install",
            },
        )
        response = await client.get(
            f"/admin/export/{project_id}?format=csv", headers=_headers(admin_token)
        )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    names = set(archive.namelist())
    assert {"project.csv", "commitments.csv", "evidence.csv", "budgets.csv", "audit_log.csv"} <= names

    commitments_csv = list(csv.DictReader(io.StringIO(archive.read("commitments.csv").decode())))
    assert len(commitments_csv) == 1
    assert commitments_csv[0]["deliverable_en"] == "LED screen install"


@pytest.mark.asyncio
async def test_export_nonexistent_project_404s(authed_org_and_project):
    org_id, project_id, _admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/admin/export/{uuid.uuid4()}", headers=_headers(admin_token))
    assert response.status_code == 404


# --- GET /admin/cost-summary (NFR-OBS-03 / PRD §13) ------------------------


async def _log_usage(app_session, org_id, project_id, *, provider, model, tokens_in, tokens_out, cost):
    await set_org_context(app_session, org_id)
    app_session.add(
        LLMUsageEvent(
            organisation_id=org_id, project_id=project_id, role="extraction", purpose="test",
            provider=provider, model=model, tokens_in=tokens_in, tokens_out=tokens_out,
            estimated_cost_usd=cost,
        )
    )
    await app_session.commit()


@pytest.mark.asyncio
async def test_cost_summary_aggregates_by_project_provider_and_model(
    app_session, authed_org_and_project
):
    org_id, project_id, _admin, admin_token = authed_org_and_project
    await _log_usage(
        app_session, org_id, project_id,
        provider="ollama", model="qwen2.5:14b", tokens_in=100, tokens_out=50, cost=0.0,
    )
    await _log_usage(
        app_session, org_id, project_id,
        provider="ollama", model="qwen2.5:14b", tokens_in=200, tokens_out=80, cost=0.0,
    )
    await _log_usage(
        app_session, org_id, project_id,
        provider="anthropic", model="claude-haiku-4-5", tokens_in=1000, tokens_out=200, cost=0.002,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/admin/cost-summary", headers=_headers(admin_token))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total_calls"] == 3
    assert body["total_estimated_cost_usd"] == pytest.approx(0.002)

    rows = {(r["provider"], r["model"]): r for r in body["rows"]}
    ollama_row = rows[("ollama", "qwen2.5:14b")]
    assert ollama_row["call_count"] == 2
    assert ollama_row["tokens_in"] == 300
    assert ollama_row["tokens_out"] == 130
    assert ollama_row["estimated_cost_usd"] == pytest.approx(0.0)

    anthropic_row = rows[("anthropic", "claude-haiku-4-5")]
    assert anthropic_row["call_count"] == 1
    assert anthropic_row["estimated_cost_usd"] == pytest.approx(0.002)


@pytest.mark.asyncio
async def test_cost_summary_filters_by_project(app_session, authed_org_and_project):
    org_id, project_id, _admin, admin_token = authed_org_and_project
    other_project_id = uuid.uuid4()
    await set_org_context(app_session, org_id)
    existing_project = await app_session.get(Project, project_id)
    app_session.add(
        Project(
            id=other_project_id, organisation_id=org_id, vertical_id=existing_project.vertical_id,
            name="Other Project", timezone="Asia/Singapore",
        )
    )
    await app_session.commit()
    await _log_usage(
        app_session, org_id, project_id, provider="ollama", model="qwen2.5:14b",
        tokens_in=10, tokens_out=5, cost=0.0,
    )
    await _log_usage(
        app_session, org_id, other_project_id, provider="ollama", model="qwen2.5:14b",
        tokens_in=999, tokens_out=999, cost=0.0,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/admin/cost-summary", params={"project_id": str(project_id)}, headers=_headers(admin_token)
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total_calls"] == 1
    assert body["rows"][0]["tokens_in"] == 10


@pytest.mark.asyncio
async def test_cost_summary_with_no_usage_reports_none_not_zero(authed_org_and_project):
    _org_id, _project_id, _admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/admin/cost-summary", headers=_headers(admin_token))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {"rows": [], "total_calls": 0, "total_estimated_cost_usd": None}


@pytest.mark.asyncio
async def test_cost_summary_requires_org_administrator(app_session, authed_org_and_project):
    org_id, project_id, admin, _admin_token = authed_org_and_project
    _pm, pm_token = await _member(app_session, org_id, project_id, "project_manager", admin.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/admin/cost-summary", headers=_headers(pm_token))

    assert response.status_code == 403
