"""Coverage for the post-Prompt-4 Twin expansion: archetype tier resolution
(universal/vertical/tenant, CUE-PRD.md §4.2.1, mirrored onto MilestoneArchetype
per app/twin/service.py's _resolve_archetype), explicit template dependency
edges replacing the old implied chain, `archetype_code` selection at project
creation, and the mutable project-level milestone/dependency CRUD
(app/api/milestones.py's POST/DELETE).
"""

import uuid
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.identity.config import get_identity_settings
from app.identity.models import Membership, User
from app.models import Milestone
from app.twin.models import Dependency, MilestoneArchetype, MilestoneArchetypeItem, TwinAuditLog
from main import app
from tests.conftest import set_org_context

EVENT_PRODUCTION_VERTICAL_CODE = "event-production"


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
    from tests.conftest import mint_token

    return user, mint_token(org_id, subject=subject, email=user.email)


async def _create_project(client, token, **overrides) -> dict:
    body = {"name": "Expansion test project", **overrides}
    response = await client.post("/projects", headers=_headers(token), json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def _event_production_vertical_id(session) -> uuid.UUID:
    from sqlalchemy import text

    return (
        await session.execute(
            text("SELECT id FROM verticals WHERE code = :code"), {"code": EVENT_PRODUCTION_VERTICAL_CODE}
        )
    ).scalar_one()


# --- Explicit template dependency edges (not an implied chain) -------------


@pytest.mark.asyncio
async def test_materialized_edges_match_seed_archetype_shape(authed_org_and_project):
    org_id, _existing, user, token = authed_org_and_project
    event_start = datetime(2026, 6, 24, tzinfo=timezone.utc)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project = await _create_project(client, token, event_start=event_start.isoformat())
        milestones = (
            await client.get(f"/projects/{project['id']}/milestones", headers=_headers(token))
        ).json()
        dependencies = (
            await client.get(
                f"/projects/{project['id']}/milestones/dependencies", headers=_headers(token)
            )
        ).json()

    by_planned_at = {m["id"]: datetime.fromisoformat(m["planned_at"]) for m in milestones}
    doors = next(m for m in milestones if m["is_fixed"])
    assert datetime.fromisoformat(doors["planned_at"]) == event_start

    # Edge structure, not just count: doors has exactly one upstream and one
    # downstream edge in the seeded chain (rehearsal -> doors -> crate
    # collection), and no edge skips past it.
    into_doors = [d for d in dependencies if d["downstream_milestone_id"] == doors["id"]]
    out_of_doors = [d for d in dependencies if d["upstream_milestone_id"] == doors["id"]]
    assert len(into_doors) == 1
    assert len(out_of_doors) == 1
    assert by_planned_at[into_doors[0]["upstream_milestone_id"]] < by_planned_at[doors["id"]]
    assert by_planned_at[out_of_doors[0]["downstream_milestone_id"]] > by_planned_at[doors["id"]]


# --- archetype_code selection ------------------------------------------------


@pytest.mark.asyncio
async def test_archetype_code_selects_named_template(authed_org_and_project):
    org_id, _existing, user, token = authed_org_and_project
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project = await _create_project(client, token, archetype_code="event-production-default")
        milestones = (
            await client.get(f"/projects/{project['id']}/milestones", headers=_headers(token))
        ).json()
    assert len(milestones) == 18


@pytest.mark.asyncio
async def test_unknown_archetype_code_is_rejected(authed_org_and_project):
    org_id, _existing, user, token = authed_org_and_project
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/projects",
            headers=_headers(token),
            json={"name": "Bad archetype", "archetype_code": "not-a-real-archetype"},
        )
    assert response.status_code == 422
    assert "not-a-real-archetype" in response.json()["detail"]


# --- Archetype tier resolution (CUE-PRD.md §4.2.1 mirrored onto templates) --


@pytest.mark.asyncio
async def test_vertical_default_beats_bare_universal_default(app_session, authed_org_and_project):
    """A universal-tier archetype must never win over the real
    event-production vertical default that already exists — resolution
    should stay at the more specific tier."""
    org_id, _existing, user, token = authed_org_and_project
    await set_org_context(app_session, org_id)

    universal_archetype = MilestoneArchetype(
        code=f"universal-decoy-{uuid.uuid4()}", vertical_id=None, organisation_id=None,
        name="Decoy universal default", is_default=True,
    )
    app_session.add(universal_archetype)
    await app_session.flush()
    app_session.add(
        MilestoneArchetypeItem(
            archetype_id=universal_archetype.id, sequence_order=0, type_code="doors",
            name="Decoy doors", day_offset=0, is_fixed=True,
        )
    )
    await app_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project = await _create_project(client, token)
        milestones = (
            await client.get(f"/projects/{project['id']}/milestones", headers=_headers(token))
        ).json()
    assert len(milestones) == 18  # the real event-production default, not the 1-item decoy


@pytest.mark.asyncio
async def test_tenant_default_beats_vertical_default(app_session, authed_org_and_project):
    org_id, _existing, user, token = authed_org_and_project
    await set_org_context(app_session, org_id)
    vertical_id = await _event_production_vertical_id(app_session)

    tenant_archetype = MilestoneArchetype(
        code=f"tenant-{org_id}", vertical_id=vertical_id, organisation_id=org_id,
        name="This org's own default", is_default=True,
    )
    app_session.add(tenant_archetype)
    await app_session.flush()
    app_session.add(
        MilestoneArchetypeItem(
            archetype_id=tenant_archetype.id, sequence_order=0, type_code="doors",
            name="Tenant doors", day_offset=0, is_fixed=True,
        )
    )
    await app_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project = await _create_project(client, token)
        milestones = (
            await client.get(f"/projects/{project['id']}/milestones", headers=_headers(token))
        ).json()
    assert len(milestones) == 1  # the tenant's own 1-item default, not the vertical's 18


@pytest.mark.asyncio
async def test_tenant_archetype_does_not_leak_across_verticals(app_session, authed_org_and_project):
    """Regression: a tenant-tier archetype scoped to a *different* vertical
    must not be picked just because organisation_id matches — CUE-PRD.md
    §4.2.1's tenant extension is scoped "within their vertical"."""
    org_id, project_id, user, token = authed_org_and_project
    await set_org_context(app_session, org_id)

    other_vertical_id = uuid.uuid4()  # stands in for e.g. a future renovation vertical
    from app.models import Vertical

    app_session.add(Vertical(id=other_vertical_id, code=f"other-{other_vertical_id}", name="Other Vertical"))
    await app_session.flush()

    decoy = MilestoneArchetype(
        code=f"tenant-other-vertical-{org_id}", vertical_id=other_vertical_id, organisation_id=org_id,
        name="This org's OTHER vertical's default", is_default=True,
    )
    app_session.add(decoy)
    await app_session.flush()
    app_session.add(
        MilestoneArchetypeItem(
            archetype_id=decoy.id, sequence_order=0, type_code="doors", name="Decoy", day_offset=0,
            is_fixed=True,
        )
    )
    await app_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # This project is created in the event-production vertical (the
        # default vertical_code) — the decoy above must not apply to it.
        project = await _create_project(client, token)
        milestones = (
            await client.get(f"/projects/{project['id']}/milestones", headers=_headers(token))
        ).json()
    assert len(milestones) == 18


# --- Mutable project-level milestone/dependency CRUD (FR-TWN-01/10) --------


@pytest.mark.asyncio
async def test_create_milestone_via_api_is_audited(app_session, authed_org_and_project):
    org_id, project_id, admin, token = authed_org_and_project
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/milestones",
            headers=_headers(token),
            json={"type_code": "permit_issuance", "name": "Client-specific permit"},
        )
    assert response.status_code == 201, response.text
    milestone_id = response.json()["id"]

    await set_org_context(app_session, org_id)
    audit_rows = (
        await app_session.execute(
            select(TwinAuditLog).where(
                TwinAuditLog.milestone_id == uuid.UUID(milestone_id),
                TwinAuditLog.action == "milestone_created",
            )
        )
    ).scalars().all()
    assert len(audit_rows) == 1


@pytest.mark.asyncio
async def test_create_milestone_unknown_type_code_is_rejected(authed_org_and_project):
    org_id, project_id, admin, token = authed_org_and_project
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/milestones",
            headers=_headers(token),
            json={"type_code": "not_a_real_milestone_type", "name": "Bogus"},
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_create_milestone_read_only_member_is_forbidden(app_session, authed_org_and_project):
    org_id, project_id, admin, _admin_token = authed_org_and_project
    _viewer, viewer_token = await _member(app_session, org_id, project_id, "read_only", admin.id)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/projects/{project_id}/milestones",
            headers=_headers(viewer_token),
            json={"type_code": "permit_issuance", "name": "Should be forbidden"},
        )
    assert response.status_code == 403


async def _create_milestone(client, token, project_id, type_code, name=None) -> dict:
    response = await client.post(
        f"/projects/{project_id}/milestones",
        headers=_headers(token),
        json={"type_code": type_code, "name": name or type_code},
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.asyncio
async def test_delete_milestone_via_api_is_audited_and_removed(app_session, authed_org_and_project):
    org_id, project_id, admin, token = authed_org_and_project
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        milestone = await _create_milestone(client, token, project_id, "permit_issuance")
        response = await client.delete(
            f"/projects/{project_id}/milestones/{milestone['id']}", headers=_headers(token)
        )
        assert response.status_code == 204, response.text

        listing = (
            await client.get(f"/projects/{project_id}/milestones", headers=_headers(token))
        ).json()
    assert milestone["id"] not in [m["id"] for m in listing]

    await set_org_context(app_session, org_id)
    audit_rows = (
        await app_session.execute(
            select(TwinAuditLog).where(
                TwinAuditLog.action == "milestone_deleted",
                TwinAuditLog.detail["name"].astext == "permit_issuance",
            )
        )
    ).scalars().all()
    assert len(audit_rows) == 1
    assert audit_rows[0].milestone_id is None  # SET NULL once the row it documents is gone


@pytest.mark.asyncio
async def test_delete_milestone_still_referenced_by_dependency_is_409(authed_org_and_project):
    org_id, project_id, admin, token = authed_org_and_project
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        upstream = await _create_milestone(client, token, project_id, "artwork_submission")
        downstream = await _create_milestone(client, token, project_id, "test_print")
        await client.post(
            f"/projects/{project_id}/milestones/dependencies",
            headers=_headers(token),
            json={"upstream_milestone_id": upstream["id"], "downstream_milestone_id": downstream["id"]},
        )

        response = await client.delete(
            f"/projects/{project_id}/milestones/{upstream['id']}", headers=_headers(token)
        )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_create_dependency_via_api_is_audited(app_session, authed_org_and_project):
    org_id, project_id, admin, token = authed_org_and_project
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        upstream = await _create_milestone(client, token, project_id, "artwork_submission")
        downstream = await _create_milestone(client, token, project_id, "test_print")
        response = await client.post(
            f"/projects/{project_id}/milestones/dependencies",
            headers=_headers(token),
            json={
                "upstream_milestone_id": upstream["id"],
                "downstream_milestone_id": downstream["id"],
                "lag_days": 3,
            },
        )
    assert response.status_code == 201, response.text
    dependency_id = response.json()["id"]

    await set_org_context(app_session, org_id)
    audit_rows = (
        await app_session.execute(
            select(TwinAuditLog).where(
                TwinAuditLog.dependency_id == uuid.UUID(dependency_id),
                TwinAuditLog.action == "dependency_created",
            )
        )
    ).scalars().all()
    assert len(audit_rows) == 1


@pytest.mark.asyncio
async def test_create_dependency_that_would_cycle_is_rejected(authed_org_and_project):
    org_id, project_id, admin, token = authed_org_and_project
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        a = await _create_milestone(client, token, project_id, "artwork_submission")
        b = await _create_milestone(client, token, project_id, "test_print")
        first = await client.post(
            f"/projects/{project_id}/milestones/dependencies",
            headers=_headers(token),
            json={"upstream_milestone_id": a["id"], "downstream_milestone_id": b["id"]},
        )
        assert first.status_code == 201, first.text

        reversed_edge = await client.post(
            f"/projects/{project_id}/milestones/dependencies",
            headers=_headers(token),
            json={"upstream_milestone_id": b["id"], "downstream_milestone_id": a["id"]},
        )
    assert reversed_edge.status_code == 422
    assert "cycle" in reversed_edge.json()["detail"]


@pytest.mark.asyncio
async def test_delete_dependency_via_api_is_audited_and_removed(app_session, authed_org_and_project):
    org_id, project_id, admin, token = authed_org_and_project
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        a = await _create_milestone(client, token, project_id, "artwork_submission")
        b = await _create_milestone(client, token, project_id, "test_print")
        created = await client.post(
            f"/projects/{project_id}/milestones/dependencies",
            headers=_headers(token),
            json={"upstream_milestone_id": a["id"], "downstream_milestone_id": b["id"]},
        )
        dependency_id = created.json()["id"]

        response = await client.delete(
            f"/projects/{project_id}/milestones/dependencies/{dependency_id}", headers=_headers(token)
        )
        assert response.status_code == 204, response.text

        listing = (
            await client.get(f"/projects/{project_id}/milestones/dependencies", headers=_headers(token))
        ).json()
    assert dependency_id not in [d["id"] for d in listing]

    await set_org_context(app_session, org_id)
    audit_rows = (
        await app_session.execute(
            select(TwinAuditLog).where(TwinAuditLog.action == "dependency_deleted")
        )
    ).scalars().all()
    assert len(audit_rows) == 1
    assert audit_rows[0].dependency_id is None  # SET NULL once the row it documents is gone


@pytest.mark.asyncio
async def test_create_dependency_read_only_member_is_forbidden(app_session, authed_org_and_project):
    org_id, project_id, admin, admin_token = authed_org_and_project
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        a = await _create_milestone(client, admin_token, project_id, "artwork_submission")
        b = await _create_milestone(client, admin_token, project_id, "test_print")

        _viewer, viewer_token = await _member(app_session, org_id, project_id, "read_only", admin.id)
        response = await client.post(
            f"/projects/{project_id}/milestones/dependencies",
            headers=_headers(viewer_token),
            json={"upstream_milestone_id": a["id"], "downstream_milestone_id": b["id"]},
        )
    assert response.status_code == 403
