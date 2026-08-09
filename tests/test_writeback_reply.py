"""app/writeback/reply.py — FR-WBK-06 ("parse the reply into a structured
commitment state transition, in either language") and FR-WBK-07 ("fall back
gracefully ... escalate to the PM rather than guess"). Both outcomes are
correct behaviour in different situations — asserted as two separate cases,
not just the happy path, per Prompt 12's own testing expectation.
"""

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.capture.models import Message
from app.foresight.models import Notification
from app.identity.models import Membership
from app.ledger.lifecycle import TRANSITIONS
from app.models import AuditLog, Channel, ChannelIdentity, Commitment, Evidence, OntologyTerm, Party, Project
from app.writeback.models import OutboundMessage
from app.writeback.reply import handle_potential_reply
from app.writeback.service import authorise_writeback, draft_writeback, send_writeback
from tests.conftest import set_org_context


class FakeJSONClient:
    def __init__(self, response: dict):
        self.response = response
        self.calls = 0

    async def complete(self, prompt: str, schema: dict) -> str:
        self.calls += 1
        return json.dumps(self.response)


async def _act_term(app_session) -> OntologyTerm:
    return (
        await app_session.execute(
            select(OntologyTerm).where(
                OntologyTerm.category == "commitment_act", OntologyTerm.code == "commit",
                OntologyTerm.vertical_id.is_(None), OntologyTerm.organisation_id.is_(None),
            )
        )
    ).scalar_one()


async def _seed_sent_outbound(
    app_session, org_id, project_id, actor_id, *, commitment_state="committed"
) -> tuple[Channel, Commitment, OutboundMessage]:
    await set_org_context(app_session, org_id)
    channel = Channel(project_id=project_id, type="whatsapp", external_ref="reply-group")
    vendor = Party(organisation_id=org_id, display_name="Reply Vendor", type="vendor_org")
    internal = Party(organisation_id=org_id, display_name="Pico Team", type="internal_staff")
    app_session.add_all([channel, vendor, internal])
    await app_session.flush()
    app_session.add(ChannelIdentity(party_id=vendor.id, channel_type="whatsapp", external_id="+65-1111-2222"))
    await app_session.flush()

    act_term = await _act_term(app_session)
    commitment = Commitment(
        project_id=project_id, party_id=vendor.id, counterparty_id=internal.id, act_type_id=act_term.id,
        state=commitment_state, deliverable_en="LED screen install", confidence=0.9, field_confidence={},
        verification_state="auto",
    )
    app_session.add(commitment)
    await app_session.flush()

    founding_text = "we will install the LED screen"
    founding_message = Message(
        project_id=project_id, channel_id=channel.id, external_id=f"msg-{uuid.uuid4()}",
        sender_external_id="+65-1111-2222", author_party_id=vendor.id,
        sent_at=datetime.now(timezone.utc) - timedelta(hours=1), language="en", text=founding_text,
        payload_hash=f"hash-{uuid.uuid4()}",
    )
    app_session.add(founding_message)
    await app_session.flush()
    app_session.add(
        Evidence(
            commitment_id=commitment.id, message_id=founding_message.id, channel="whatsapp",
            sent_at=founding_message.sent_at, language="en", original_text=founding_text,
            span_start=0, span_end=len(founding_text),
        )
    )
    await app_session.commit()

    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    outbound = await draft_writeback(
        app_session, project=project, commitment=commitment,
        client=FakeJSONClient({"question": "Is the install still confirmed for Tuesday?"}),
    )
    await app_session.commit()
    outbound = await authorise_writeback(app_session, outbound=outbound, actor_id=actor_id)
    await app_session.commit()
    outbound = await send_writeback(app_session, project=project, outbound=outbound, actor_id=actor_id)
    await app_session.commit()
    return channel, commitment, outbound


async def _inbound_reply(app_session, project_id, channel: Channel, vendor_party_id, text: str, after: datetime) -> Message:
    message = Message(
        project_id=project_id, channel_id=channel.id, external_id=f"reply-{uuid.uuid4()}",
        sender_external_id="+65-1111-2222", author_party_id=vendor_party_id,
        sent_at=after + timedelta(minutes=5), language="en", text=text,
        payload_hash=f"hash-{uuid.uuid4()}",
    )
    app_session.add(message)
    await app_session.commit()
    return message


@pytest.mark.asyncio
async def test_no_pending_outbound_is_a_no_op(app_session, org_and_project, seeded_user):
    """The common case — most inbound messages are not replies to a pending
    write-back — never even calls the LLM."""
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    channel = Channel(project_id=project_id, type="whatsapp", external_ref="no-pending-group")
    app_session.add(channel)
    await app_session.commit()

    message = Message(
        project_id=project_id, channel_id=channel.id, external_id=f"msg-{uuid.uuid4()}",
        sender_external_id="+65-0000-0000", sent_at=datetime.now(timezone.utc), language="en",
        text="just chatting, not a reply to anything", payload_hash=f"hash-{uuid.uuid4()}",
    )
    app_session.add(message)
    await app_session.commit()

    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    result = await handle_potential_reply(app_session, project=project, channel_id=channel.id, message=message)
    assert result.matched_outbound is False
    assert result.outcome is None


@pytest.mark.asyncio
async def test_parseable_reply_transitions_the_commitment(app_session, org_and_project, seeded_user):
    """FR-WBK-06: a clear reply resolves to a real, validated state
    transition — going through the same validate_transition/audit/Twin
    recompute path a manual transition would."""
    org_id, project_id = org_and_project
    channel, commitment, outbound = await _seed_sent_outbound(app_session, org_id, project_id, seeded_user.id)
    assert commitment.state == "committed"
    assert "delivered" in TRANSITIONS["committed"]

    reply = await _inbound_reply(
        app_session, project_id, channel, commitment.party_id, "Yes, confirmed.", after=outbound.sent_at
    )
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()

    client = FakeJSONClient({"parseable": True, "to_state": "delivered", "reasoning": "vendor confirmed"})
    result = await handle_potential_reply(
        app_session, project=project, channel_id=channel.id, message=reply, client=client
    )
    await app_session.commit()

    assert result.matched_outbound is True
    assert result.outcome == "transitioned"
    assert client.calls == 1

    await app_session.refresh(commitment)
    assert commitment.state == "delivered"

    await app_session.refresh(outbound)
    assert outbound.reply_outcome == "transitioned"
    assert outbound.reply_message_id == reply.id

    audit_rows = (
        await app_session.execute(
            select(AuditLog).where(
                AuditLog.commitment_id == commitment.id, AuditLog.action == "state_transition",
                AuditLog.to_state == "delivered",
            )
        )
    ).scalars().all()
    assert len(audit_rows) == 1
    assert audit_rows[0].detail["outbound_message_id"] == str(outbound.id)
    assert audit_rows[0].actor_id is None  # system-driven, per app/ledger/extractor.py's own convention


@pytest.mark.asyncio
async def test_unparseable_reply_escalates_to_the_owning_pm(app_session, org_and_project, seeded_user):
    """FR-WBK-07: never guess. An ambiguous reply escalates instead of
    silently transitioning anything."""
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    app_session.add(
        Membership(user_id=seeded_user.id, project_id=project_id, role="project_manager", granted_by=seeded_user.id)
    )
    await app_session.commit()

    channel, commitment, outbound = await _seed_sent_outbound(app_session, org_id, project_id, seeded_user.id)
    reply = await _inbound_reply(
        app_session, project_id, channel, commitment.party_id, "let me check and get back to you",
        after=outbound.sent_at,
    )
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()

    client = FakeJSONClient({"parseable": False, "to_state": None, "reasoning": "non-committal reply"})
    result = await handle_potential_reply(
        app_session, project=project, channel_id=channel.id, message=reply, client=client
    )
    await app_session.commit()

    assert result.outcome == "escalated"

    await app_session.refresh(commitment)
    assert commitment.state == "committed"  # unchanged — never guessed

    await app_session.refresh(outbound)
    assert outbound.reply_outcome == "escalated"

    notifications = (
        await app_session.execute(
            select(Notification).where(
                Notification.project_id == project_id, Notification.commitment_id == commitment.id
            )
        )
    ).scalars().all()
    assert len(notifications) == 1
    assert notifications[0].recipient_id == seeded_user.id
    assert notifications[0].detail["event_type"] == "writeback_reply_escalation"


@pytest.mark.asyncio
async def test_reply_implying_invalid_transition_escalates_not_forces(app_session, org_and_project, seeded_user):
    """A reply that would imply an invalid transition is a parse failure to
    escalate, not a transition to force through (Prompt 12's own wording) —
    'delivered' is a terminal state (TRANSITIONS['delivered'] is empty)."""
    org_id, project_id = org_and_project
    channel, commitment, outbound = await _seed_sent_outbound(
        app_session, org_id, project_id, seeded_user.id, commitment_state="delivered"
    )
    assert TRANSITIONS["delivered"] == frozenset()

    reply = await _inbound_reply(
        app_session, project_id, channel, commitment.party_id, "actually please cancel this",
        after=outbound.sent_at,
    )
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()

    client = FakeJSONClient({"parseable": True, "to_state": "withdrawn", "reasoning": "vendor asked to cancel"})
    result = await handle_potential_reply(
        app_session, project=project, channel_id=channel.id, message=reply, client=client
    )
    await app_session.commit()

    assert result.outcome == "escalated"
    await app_session.refresh(commitment)
    assert commitment.state == "delivered"  # never force-transitioned

    await app_session.refresh(outbound)
    assert outbound.reply_outcome == "escalated"
    assert "invalid transition" in outbound.reply_detail["reason"]
