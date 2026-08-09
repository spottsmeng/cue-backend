"""REST endpoints for /projects/{id}/writeback — draft · authorise · send ·
history · config (PRD §11.2). RLS and role-gating tested as two independent
properties, same convention tests/test_risks_api.py already establishes.

The draft endpoint's own LLM call is monkeypatched at the same seam
app/writeback/compose.py calls (app.llm.factory.get_client, bound into that
module's own namespace) — "fakes injected below the HTTP layer", the same
idiom tests/test_ask_api.py's own module docstring documents, so this suite
needs no live Ollama/Anthropic.
"""

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.capture.models import Message
from app.identity.config import get_identity_settings
from app.identity.models import Membership, User
from app.models import Channel, ChannelIdentity, Commitment, Evidence, OntologyTerm, Party, Project
from app.writeback.models import OutboundMessage
from main import app
from tests.conftest import FAKE_LLM_USAGE, mint_token, set_org_context


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


async def _seed_draftable_commitment(app_session, org_id, project_id) -> Commitment:
    await set_org_context(app_session, org_id)
    channel = Channel(project_id=project_id, type="whatsapp", external_ref="api-test-group")
    vendor = Party(organisation_id=org_id, display_name="API Test Vendor", type="vendor_org")
    internal = Party(organisation_id=org_id, display_name="Pico Team", type="internal_staff")
    app_session.add_all([channel, vendor, internal])
    await app_session.flush()
    app_session.add(ChannelIdentity(party_id=vendor.id, channel_type="whatsapp", external_id="+65-5555-0000"))
    await app_session.flush()

    act_term = (
        await app_session.execute(
            select(OntologyTerm).where(
                OntologyTerm.category == "commitment_act", OntologyTerm.code == "commit",
                OntologyTerm.vertical_id.is_(None), OntologyTerm.organisation_id.is_(None),
            )
        )
    ).scalar_one()
    commitment = Commitment(
        project_id=project_id, party_id=vendor.id, counterparty_id=internal.id, act_type_id=act_term.id,
        state="committed", deliverable_en="Signage install", confidence=0.9, field_confidence={},
        verification_state="auto",
    )
    app_session.add(commitment)
    await app_session.flush()

    text = "we will install the signage next week"
    message = Message(
        project_id=project_id, channel_id=channel.id, external_id=f"msg-{uuid.uuid4()}",
        sender_external_id="+65-5555-0000", author_party_id=vendor.id,
        sent_at=datetime.now(timezone.utc), language="en", text=text, payload_hash=f"hash-{uuid.uuid4()}",
    )
    app_session.add(message)
    await app_session.flush()
    app_session.add(
        Evidence(
            commitment_id=commitment.id, message_id=message.id, channel="whatsapp",
            sent_at=message.sent_at, language="en", original_text=text, span_start=0, span_end=len(text),
        )
    )
    await app_session.commit()
    return commitment


class FakeJSONClient:
    def __init__(self, response: dict):
        self.response = response

    async def complete(self, prompt: str, schema: dict):
        return json.dumps(self.response), FAKE_LLM_USAGE


@pytest.mark.asyncio
async def test_read_only_member_can_view_history_but_not_draft(app_session, authed_org_and_project):
    org_id, project_id, admin, _admin_token = authed_org_and_project
    commitment = await _seed_draftable_commitment(app_session, org_id, project_id)
    _viewer, viewer_token = await _member(app_session, org_id, project_id, "read_only", admin.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        history = await client.get(f"/projects/{project_id}/writeback", headers=_headers(viewer_token))
        assert history.status_code == 200

        draft = await client.post(
            f"/projects/{project_id}/writeback/draft",
            headers=_headers(viewer_token),
            json={"commitment_id": str(commitment.id)},
        )
    assert draft.status_code == 403


@pytest.mark.asyncio
async def test_non_admin_write_role_cannot_update_ceiling(app_session, authed_org_and_project):
    """ADMIN_ROLES-gated, not WRITE_ROLES — a project_manager can draft/
    authorise/send but not change the rate ceiling itself (FR-WBK-04's own
    'explicit, audited configuration change' bar)."""
    org_id, project_id, admin, admin_token = authed_org_and_project
    _pm, pm_token = await _member(app_session, org_id, project_id, "project_manager", admin.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.patch(
            f"/projects/{project_id}/writeback/config", headers=_headers(pm_token), json={"daily_ceiling": 3}
        )
        assert denied.status_code == 403

        allowed = await client.patch(
            f"/projects/{project_id}/writeback/config", headers=_headers(admin_token), json={"daily_ceiling": 3}
        )
    assert allowed.status_code == 200
    assert allowed.json()["daily_ceiling"] == 3


@pytest.mark.asyncio
async def test_outbound_messages_are_isolated_via_project_join_rls(app_session, authed_org_and_project):
    org_id, project_id, _admin, _token = authed_org_and_project
    commitment = await _seed_draftable_commitment(app_session, org_id, project_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()

    from app.writeback.service import draft_writeback

    with patch(
        "app.writeback.compose.get_client",
        return_value=FakeJSONClient({"question": "Still on track?"}),
    ):
        outbound = await draft_writeback(app_session, project=project, commitment=commitment)
    await app_session.commit()

    await set_org_context(app_session, uuid.uuid4())  # a different, unrelated org
    result = await app_session.execute(select(OutboundMessage).where(OutboundMessage.id == outbound.id))
    assert result.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_full_draft_authorise_send_cycle_through_http(app_session, authed_org_and_project):
    org_id, project_id, _admin, token = authed_org_and_project
    commitment = await _seed_draftable_commitment(app_session, org_id, project_id)

    transport = ASGITransport(app=app)
    with patch(
        "app.writeback.compose.get_client",
        return_value=FakeJSONClient({"question": "Is the signage install still confirmed?"}),
    ):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            draft_response = await client.post(
                f"/projects/{project_id}/writeback/draft",
                headers=_headers(token),
                json={"commitment_id": str(commitment.id)},
            )
    assert draft_response.status_code == 201, draft_response.text
    draft_body = draft_response.json()
    assert draft_body["status"] == "draft"
    assert draft_body["draft_text"] == "Is the signage install still confirmed?"
    outbound_id = draft_body["id"]

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # FR-WBK-05: send before authorise is refused, not silently reordered.
        premature_send = await client.post(
            f"/projects/{project_id}/writeback/{outbound_id}/send", headers=_headers(token)
        )
        assert premature_send.status_code == 409

        authorise_response = await client.post(
            f"/projects/{project_id}/writeback/{outbound_id}/authorise", headers=_headers(token)
        )
        assert authorise_response.status_code == 200
        assert authorise_response.json()["status"] == "authorised"

        send_response = await client.post(
            f"/projects/{project_id}/writeback/{outbound_id}/send", headers=_headers(token)
        )
        assert send_response.status_code == 200
        sent_body = send_response.json()
        assert sent_body["status"] == "sent"
        assert sent_body["sent_at"] is not None

        history = await client.get(
            f"/projects/{project_id}/writeback", headers=_headers(token),
            params={"commitment_id": str(commitment.id)},
        )
    assert history.status_code == 200
    assert len(history.json()) == 1
    assert history.json()[0]["status"] == "sent"


@pytest.mark.asyncio
async def test_edit_draft_before_authorise_then_locked_after(app_session, authed_org_and_project):
    """F1 frontend-enablement addition: PATCH .../{outbound_id} — FR-WBK-05's
    "review/edit before authorisation" made real, the edit half (review
    already had a surface via GET). Editable only while status == "draft";
    once authorised, the text a human signed off on is frozen, same
    reasoning the class docstring gives for to_external_id/language/
    channel_id."""
    org_id, project_id, _admin, token = authed_org_and_project
    commitment = await _seed_draftable_commitment(app_session, org_id, project_id)

    transport = ASGITransport(app=app)
    with patch(
        "app.writeback.compose.get_client",
        return_value=FakeJSONClient({"question": "Still on track?"}),
    ):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            draft_response = await client.post(
                f"/projects/{project_id}/writeback/draft",
                headers=_headers(token),
                json={"commitment_id": str(commitment.id)},
            )
    outbound_id = draft_response.json()["id"]

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        edited = await client.patch(
            f"/projects/{project_id}/writeback/{outbound_id}",
            headers=_headers(token),
            json={"draft_text": "Confirming: install still happening Friday, yes/no?"},
        )
        assert edited.status_code == 200
        assert edited.json()["draft_text"] == "Confirming: install still happening Friday, yes/no?"

        authorise_response = await client.post(
            f"/projects/{project_id}/writeback/{outbound_id}/authorise", headers=_headers(token)
        )
        assert authorise_response.status_code == 200

        locked = await client.patch(
            f"/projects/{project_id}/writeback/{outbound_id}",
            headers=_headers(token),
            json={"draft_text": "too late"},
        )
    assert locked.status_code == 409
