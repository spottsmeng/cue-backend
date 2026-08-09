"""app/writeback/service.py — FR-WBK-01 through 05/08's draft -> authorise ->
send cycle. Real Postgres throughout (tests/conftest.py's app_session), a
fake LLM client injected at the service boundary (same shape
tests/test_document_extractor.py's FakeModelClient / tests/test_ask_answer.py
already establish) so this suite needs no live Ollama/Anthropic.

FR-WBK-05's "never post autonomously" is asserted directly: a draft cannot
be sent without a prior, separate authorise call (test_send_requires_prior_authorise).
"""

import json
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models import AuditLog, Channel, ChannelIdentity, Commitment, Evidence, OntologyTerm, Party, Project
from app.capture.models import Message
from app.writeback.compose import ComposeError
from app.writeback.service import (
    InvalidWritebackTransition,
    WritebackTargetUnresolved,
    authorise_writeback,
    draft_writeback,
    send_writeback,
)
from tests.conftest import FAKE_LLM_USAGE, set_org_context


class FakeJSONClient:
    def __init__(self, response: dict):
        self.response = response
        self.calls = 0

    async def complete(self, prompt: str, schema: dict):
        self.calls += 1
        return json.dumps(self.response), FAKE_LLM_USAGE


async def _act_term(app_session, code: str) -> OntologyTerm:
    return (
        await app_session.execute(
            select(OntologyTerm).where(
                OntologyTerm.category == "commitment_act",
                OntologyTerm.code == code,
                OntologyTerm.vertical_id.is_(None),
                OntologyTerm.organisation_id.is_(None),
            )
        )
    ).scalar_one()


async def _seed_commitment_with_evidence(
    app_session, org_id, project_id, *, message_text="我们会安装LED屏幕", payload_hash=None,
    external_ref="whatsapp-group-1", vendor_external_id="+65-9000-0001", commitment_state="committed",
) -> tuple[Channel, Party, Commitment]:
    """A real Channel + Party + ChannelIdentity + captured Message + Evidence
    + Commitment, wired the way app/capture/pipeline.py's real-capture path
    actually produces them — FR-WBK-01's "originating vendor group"
    resolution (app/writeback/service.py's _resolve_writeback_target) reads
    exactly this shape."""
    await set_org_context(app_session, org_id)
    channel = Channel(project_id=project_id, type="whatsapp", external_ref=external_ref)
    app_session.add(channel)
    await app_session.flush()

    vendor = Party(organisation_id=org_id, display_name="AV Vendor Co", type="vendor_org")
    internal = Party(organisation_id=org_id, display_name="Pico Project Team", type="internal_staff")
    app_session.add_all([vendor, internal])
    await app_session.flush()

    app_session.add(
        ChannelIdentity(party_id=vendor.id, channel_type="whatsapp", external_id=vendor_external_id)
    )
    await app_session.flush()

    act_term = await _act_term(app_session, "commit")
    commitment = Commitment(
        project_id=project_id, party_id=vendor.id, counterparty_id=internal.id, act_type_id=act_term.id,
        state=commitment_state, deliverable_en="LED screen install", deliverable_original="安装LED屏幕",
        confidence=0.9, field_confidence={}, verification_state="auto",
    )
    app_session.add(commitment)
    await app_session.flush()

    message = Message(
        project_id=project_id, channel_id=channel.id, external_id=f"msg-{uuid.uuid4()}",
        sender_external_id=vendor_external_id, author_party_id=vendor.id,
        sent_at=datetime.now(timezone.utc), language="zh", text=message_text,
        payload_hash=payload_hash or f"hash-{uuid.uuid4()}",
    )
    app_session.add(message)
    await app_session.flush()

    app_session.add(
        Evidence(
            commitment_id=commitment.id, message_id=message.id, channel="whatsapp",
            sent_at=message.sent_at, language="zh", original_text=message_text,
            span_start=0, span_end=len(message_text),
        )
    )
    await app_session.flush()
    await app_session.commit()
    return channel, vendor, commitment


@pytest.mark.asyncio
async def test_draft_composes_in_group_prevailing_language(app_session, org_and_project):
    org_id, project_id = org_and_project
    _channel, _vendor, commitment = await _seed_commitment_with_evidence(app_session, org_id, project_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()

    client = FakeJSONClient({"question": "LED屏幕安装仍按计划进行吗？"})
    outbound = await draft_writeback(app_session, project=project, commitment=commitment, client=client)
    await app_session.commit()

    assert outbound.status == "draft"
    assert outbound.commitment_id == commitment.id
    assert outbound.draft_text == "LED屏幕安装仍按计划进行吗？"
    # The seeded message was Chinese — FR-WBK-02's "group's prevailing traffic".
    assert outbound.language in ("zh", "zh+en")
    assert outbound.to_external_id == "+65-9000-0001"
    assert client.calls == 1


@pytest.mark.asyncio
async def test_draft_rejects_non_question_output(app_session, org_and_project):
    """CLAUDE.md's 'verified in code, not trusted' discipline, applied to
    FR-WBK-03: a model output that isn't phrased as a question is never
    silently accepted as a draft."""
    org_id, project_id = org_and_project
    _channel, _vendor, commitment = await _seed_commitment_with_evidence(app_session, org_id, project_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()

    client = FakeJSONClient({"question": "The install is confirmed for Tuesday."})
    with pytest.raises(ComposeError):
        await draft_writeback(app_session, project=project, commitment=commitment, client=client)


@pytest.mark.asyncio
async def test_draft_without_real_capture_evidence_is_unresolved(app_session, org_and_project):
    """FR-WBK-01's 'originating vendor group' has nothing to resolve against
    for a fixture-derived/manually-entered commitment — a clean 422-shaped
    error, not a guess."""
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    vendor = Party(organisation_id=org_id, display_name="No Evidence Vendor", type="vendor_org")
    internal = Party(organisation_id=org_id, display_name="Pico Team", type="internal_staff")
    app_session.add_all([vendor, internal])
    await app_session.flush()
    act_term = await _act_term(app_session, "commit")
    commitment = Commitment(
        project_id=project_id, party_id=vendor.id, counterparty_id=internal.id, act_type_id=act_term.id,
        state="committed", deliverable_en="Signage install", confidence=0.9, field_confidence={},
        verification_state="auto",
    )
    app_session.add(commitment)
    await app_session.flush()
    app_session.add(
        Evidence(
            commitment_id=commitment.id, channel="manual", sent_at=datetime.now(timezone.utc),
            language="en", original_text="Manually entered.",
        )
    )
    await app_session.commit()
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()

    with pytest.raises(WritebackTargetUnresolved):
        await draft_writeback(
            app_session, project=project, commitment=commitment, client=FakeJSONClient({"question": "OK?"})
        )


@pytest.mark.asyncio
async def test_send_requires_prior_authorise(app_session, org_and_project):
    """FR-WBK-05: 'never post autonomously' — draft and send are structurally
    two different calls; send refuses a draft that skipped authorise."""
    org_id, project_id = org_and_project
    _channel, _vendor, commitment = await _seed_commitment_with_evidence(app_session, org_id, project_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    client = FakeJSONClient({"question": "Confirmed?"})
    outbound = await draft_writeback(app_session, project=project, commitment=commitment, client=client)
    await app_session.commit()

    actor_id = uuid.uuid4()
    with pytest.raises(InvalidWritebackTransition):
        await send_writeback(app_session, project=project, outbound=outbound, actor_id=actor_id)

    await app_session.refresh(outbound)
    assert outbound.status == "draft"
    assert outbound.sent_at is None


@pytest.mark.asyncio
async def test_authorise_requires_draft_status(app_session, org_and_project, seeded_user):
    org_id, project_id = org_and_project
    _channel, _vendor, commitment = await _seed_commitment_with_evidence(app_session, org_id, project_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    client = FakeJSONClient({"question": "Confirmed?"})
    outbound = await draft_writeback(app_session, project=project, commitment=commitment, client=client)
    await authorise_writeback(app_session, outbound=outbound, actor_id=seeded_user.id)
    await app_session.commit()

    with pytest.raises(InvalidWritebackTransition):
        await authorise_writeback(app_session, outbound=outbound, actor_id=seeded_user.id)


@pytest.mark.asyncio
async def test_full_draft_authorise_send_cycle_logs_audit_event(app_session, org_and_project, seeded_user):
    org_id, project_id = org_and_project
    _channel, _vendor, commitment = await _seed_commitment_with_evidence(app_session, org_id, project_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()

    client = FakeJSONClient({"question": "确认吗？"})
    outbound = await draft_writeback(app_session, project=project, commitment=commitment, client=client)
    await app_session.commit()

    outbound = await authorise_writeback(app_session, outbound=outbound, actor_id=seeded_user.id)
    await app_session.commit()
    assert outbound.status == "authorised"
    assert outbound.authorised_by == seeded_user.id
    assert outbound.authorised_at is not None

    outbound = await send_writeback(app_session, project=project, outbound=outbound, actor_id=seeded_user.id)
    await app_session.commit()
    assert outbound.status == "sent"
    assert outbound.sent_at is not None
    assert outbound.rate_limit_bucket is not None

    # FR-WBK-08: logged via the shared, commitment-scoped audit trail, with
    # a dedicated action value rather than an overloaded existing one.
    audit_rows = (
        await app_session.execute(
            select(AuditLog).where(
                AuditLog.commitment_id == commitment.id, AuditLog.action == "outbound_sent"
            )
        )
    ).scalars().all()
    assert len(audit_rows) == 1
    assert audit_rows[0].actor_id == seeded_user.id
    assert audit_rows[0].detail["outbound_message_id"] == str(outbound.id)
