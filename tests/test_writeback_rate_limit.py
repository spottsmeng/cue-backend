"""FR-WBK-04: "Rate-limit outbound messages per group (default one per day,
configurable, hard ceiling enforced)." CLAUDE.md's non-obvious note: no code
path, including a race between two concurrent sends, may exceed it. The
race test below uses two genuinely independent DB sessions (two real
asyncpg connections) driven concurrently via asyncio.gather — not a mock —
so app/writeback/rate_limit.py's `SELECT ... FOR UPDATE` is actually
exercised under real Postgres row-lock contention.
"""

import asyncio
import json
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.capture.models import Message
from app.core.db import async_session_factory
from app.models import Channel, ChannelIdentity, Commitment, Evidence, OntologyTerm, Party, Project
from app.writeback.models import OutboundMessage
from app.writeback.rate_limit import RateCeilingExceeded
from app.writeback.service import authorise_writeback, draft_writeback, send_writeback
from tests.conftest import FAKE_LLM_USAGE, set_org_context


class FakeJSONClient:
    def __init__(self, response: dict):
        self.response = response

    async def complete(self, prompt: str, schema: dict):
        return json.dumps(self.response), FAKE_LLM_USAGE


async def _act_term(app_session) -> OntologyTerm:
    return (
        await app_session.execute(
            select(OntologyTerm).where(
                OntologyTerm.category == "commitment_act", OntologyTerm.code == "commit",
                OntologyTerm.vertical_id.is_(None), OntologyTerm.organisation_id.is_(None),
            )
        )
    ).scalar_one()


async def _seed_channel_and_vendor(app_session, org_id, project_id) -> tuple[Channel, Party, Party]:
    await set_org_context(app_session, org_id)
    channel = Channel(project_id=project_id, type="whatsapp", external_ref="rate-limit-group")
    vendor = Party(organisation_id=org_id, display_name="Rate Limit Vendor", type="vendor_org")
    internal = Party(organisation_id=org_id, display_name="Pico Team", type="internal_staff")
    app_session.add_all([channel, vendor, internal])
    await app_session.flush()
    app_session.add(
        ChannelIdentity(party_id=vendor.id, channel_type="whatsapp", external_id="+65-9000-9999")
    )
    await app_session.commit()
    return channel, vendor, internal


async def _seed_authorised_outbound(
    app_session, org_id, project_id, channel: Channel, vendor: Party, internal: Party, actor_id: uuid.UUID
) -> OutboundMessage:
    """A fresh Commitment + Message + Evidence on the given (shared) channel,
    drafted and authorised — ready to send."""
    await set_org_context(app_session, org_id)
    act_term = await _act_term(app_session)
    commitment = Commitment(
        project_id=project_id, party_id=vendor.id, counterparty_id=internal.id, act_type_id=act_term.id,
        state="committed", deliverable_en="LED screen install", confidence=0.9, field_confidence={},
        verification_state="auto",
    )
    app_session.add(commitment)
    await app_session.flush()

    text = f"confirming install #{uuid.uuid4()}"
    message = Message(
        project_id=project_id, channel_id=channel.id, external_id=f"msg-{uuid.uuid4()}",
        sender_external_id="+65-9000-9999", author_party_id=vendor.id,
        sent_at=datetime.now(timezone.utc), language="en", text=text,
        payload_hash=f"hash-{uuid.uuid4()}",
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

    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    outbound = await draft_writeback(
        app_session, project=project, commitment=commitment, client=FakeJSONClient({"question": "Confirmed?"})
    )
    await app_session.commit()
    outbound = await authorise_writeback(app_session, outbound=outbound, actor_id=actor_id)
    await app_session.commit()
    return outbound


@pytest.mark.asyncio
async def test_ceiling_blocks_second_send_same_day(app_session, org_and_project, seeded_user):
    org_id, project_id = org_and_project
    channel, vendor, internal = await _seed_channel_and_vendor(app_session, org_id, project_id)
    outbound_a = await _seed_authorised_outbound(app_session, org_id, project_id, channel, vendor, internal, seeded_user.id)
    outbound_b = await _seed_authorised_outbound(app_session, org_id, project_id, channel, vendor, internal, seeded_user.id)

    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    assert project.writeback_daily_ceiling == 1

    await send_writeback(app_session, project=project, outbound=outbound_a, actor_id=seeded_user.id)
    await app_session.commit()

    with pytest.raises(RateCeilingExceeded):
        await send_writeback(app_session, project=project, outbound=outbound_b, actor_id=seeded_user.id)
    await app_session.rollback()

    await app_session.refresh(outbound_b)
    assert outbound_b.status == "authorised"  # never consumed a slot, still sendable later


@pytest.mark.asyncio
async def test_configurable_ceiling_allows_more_per_day(app_session, org_and_project, seeded_user):
    org_id, project_id = org_and_project
    channel, vendor, internal = await _seed_channel_and_vendor(app_session, org_id, project_id)
    outbound_a = await _seed_authorised_outbound(app_session, org_id, project_id, channel, vendor, internal, seeded_user.id)
    outbound_b = await _seed_authorised_outbound(app_session, org_id, project_id, channel, vendor, internal, seeded_user.id)

    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    project.writeback_daily_ceiling = 2
    await app_session.commit()

    await send_writeback(app_session, project=project, outbound=outbound_a, actor_id=seeded_user.id)
    await app_session.commit()
    await send_writeback(app_session, project=project, outbound=outbound_b, actor_id=seeded_user.id)
    await app_session.commit()

    await app_session.refresh(outbound_a)
    await app_session.refresh(outbound_b)
    assert outbound_a.status == "sent"
    assert outbound_b.status == "sent"


async def _send_via_independent_session(org_id, project_id, outbound_id, actor_id) -> str:
    """A genuinely separate AsyncSession (its own asyncpg connection), same
    factory production uses (app/core/db.py's async_session_factory) — not
    the shared `app_session` fixture — so the two concurrent sends below
    really do race over two different Postgres backends/transactions."""
    async with async_session_factory() as session:
        await set_org_context(session, org_id)
        project = (await session.execute(select(Project).where(Project.id == project_id))).scalar_one()
        outbound = (
            await session.execute(select(OutboundMessage).where(OutboundMessage.id == outbound_id))
        ).scalar_one()
        try:
            await send_writeback(session, project=project, outbound=outbound, actor_id=actor_id)
            await session.commit()
            return "sent"
        except RateCeilingExceeded:
            await session.rollback()
            return "ceiling_exceeded"


@pytest.mark.asyncio
async def test_concurrent_sends_only_one_succeeds_once_ceiling_is_hit(
    app_session, org_and_project, seeded_user
):
    """The race-condition case CLAUDE.md/Prompt 12 explicitly ask for: two
    concurrent send attempts against a ceiling of 1, only one should
    succeed — proven with real concurrency, not sequential calls."""
    org_id, project_id = org_and_project
    channel, vendor, internal = await _seed_channel_and_vendor(app_session, org_id, project_id)
    outbound_a = await _seed_authorised_outbound(app_session, org_id, project_id, channel, vendor, internal, seeded_user.id)
    outbound_b = await _seed_authorised_outbound(app_session, org_id, project_id, channel, vendor, internal, seeded_user.id)

    results = await asyncio.gather(
        _send_via_independent_session(org_id, project_id, outbound_a.id, seeded_user.id),
        _send_via_independent_session(org_id, project_id, outbound_b.id, seeded_user.id),
    )

    assert sorted(results) == ["ceiling_exceeded", "sent"]

    sent_count = (
        await app_session.execute(
            select(OutboundMessage).where(
                OutboundMessage.channel_id == channel.id, OutboundMessage.status == "sent"
            )
        )
    ).scalars().all()
    assert len(sent_count) == 1
