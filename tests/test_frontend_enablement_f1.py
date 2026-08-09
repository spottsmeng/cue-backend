"""Two small, scoped backend additions made while starting `Prompt F1 —
Living WIP, Verification and Write-back.txt` (frontend), same "close the gap
you find, document it" pattern as the five post-M10 additions
`backend/PROGRESS.md` already lists:

1. `EvidenceOut.media_ref` — the column existed since M8's capture pipeline
   set it, but no response schema ever exposed it, so FR-VOI-05 ("retain
   original audio and make it playable from any evidence link") had no API
   surface at all.
2. `GET /projects/{project_id}/members/me` — F1's verification/payment-
   status/budget UI needs to know which controls to *show* the current user
   (`require_project_role`'s own "UX nicety, not a security boundary"
   position); nothing let a non-admin caller learn their own effective role
   set on a project before this.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from sqlalchemy import select

from app.identity.config import get_identity_settings
from app.identity.models import Membership, User
from app.models import Evidence, Party
from main import app
from tests.conftest import mint_token, set_org_context


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_evidence_out_exposes_media_ref(authed_org_and_project, app_session):
    org_id, project_id, _admin, token = authed_org_and_project
    await set_org_context(app_session, org_id)
    vendor = Party(organisation_id=org_id, display_name="Voice Vendor", type="vendor_org")
    internal = Party(organisation_id=org_id, display_name="Internal", type="internal_staff")
    app_session.add_all([vendor, internal])
    await app_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post(
            f"/projects/{project_id}/commitments",
            headers=_headers(token),
            json={
                "party_id": str(vendor.id),
                "counterparty_id": str(internal.id),
                "act_type": "commit",
                "deliverable_en": "LED screen install",
            },
        )
        assert create_resp.status_code == 201, create_resp.text
        commitment_id = create_resp.json()["id"]

        # Manually-created commitments get a text-only Evidence row from the
        # API itself; attach a media_ref directly the way app/capture/
        # pipeline.py's voice-note path does, to exercise the read side.
        evidence = (
            await app_session.execute(
                select(Evidence).where(Evidence.commitment_id == uuid.UUID(commitment_id))
            )
        ).scalar_one()
        evidence.media_ref = "https://storage.example.test/signed/voice-note.ogg?exp=123"
        await app_session.commit()

        read_resp = await client.get(
            f"/projects/{project_id}/commitments/{commitment_id}", headers=_headers(token)
        )
    assert read_resp.status_code == 200
    evidence_out = read_resp.json()["evidence"][0]
    assert evidence_out["media_ref"] == "https://storage.example.test/signed/voice-note.ogg?exp=123"


@pytest.mark.asyncio
async def test_evidence_out_media_ref_null_when_absent(authed_org_and_project, parties):
    org_id, project_id, _admin, token = authed_org_and_project
    vendor, internal = parties
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post(
            f"/projects/{project_id}/commitments",
            headers=_headers(token),
            json={
                "party_id": str(vendor.id),
                "counterparty_id": str(internal.id),
                "act_type": "commit",
                "deliverable_en": "text-only commitment",
            },
        )
    assert create_resp.status_code == 201, create_resp.text
    assert create_resp.json()["evidence"][0]["media_ref"] is None


@pytest.mark.asyncio
async def test_members_me_returns_own_membership_role(authed_org_and_project):
    org_id, project_id, admin, token = authed_org_and_project
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/projects/{project_id}/members/me", headers=_headers(token))
    assert resp.status_code == 200
    assert resp.json() == {"roles": ["administrator"]}


@pytest.mark.asyncio
async def test_members_me_reflects_non_admin_role(app_session, authed_org_and_project):
    org_id, project_id, admin, _admin_token = authed_org_and_project
    await set_org_context(app_session, org_id)
    subject = f"finance-{uuid.uuid4()}"
    user = User(
        organisation_id=org_id,
        issuer=get_identity_settings().local_issuer,
        external_subject=subject,
        email=f"{subject}@example.test",
    )
    app_session.add(user)
    await app_session.flush()
    app_session.add(
        Membership(user_id=user.id, project_id=project_id, role="finance", granted_by=admin.id)
    )
    await app_session.commit()
    token = mint_token(org_id, subject=subject, email=user.email)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/projects/{project_id}/members/me", headers=_headers(token))
    assert resp.status_code == 200
    assert resp.json() == {"roles": ["finance"]}


@pytest.mark.asyncio
async def test_members_me_404s_for_non_member(authed_org_and_project):
    """Same "no membership looks identical to a nonexistent project" rule
    `require_project_role`'s own docstring states for every other
    project-scoped endpoint — this one is no exception, on purpose. Reuses
    `authed_org_and_project`'s real, already-provisioned org (an org that
    doesn't exist at all would 500 on a FK violation resolving the caller's
    own `users` row, a different failure mode this test isn't about) with a
    project id that was never created in it."""
    org_id, _project_id, _admin, token = authed_org_and_project
    other_project_id = uuid.uuid4()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            f"/projects/{other_project_id}/members/me", headers=_headers(token)
        )
    assert resp.status_code == 404
