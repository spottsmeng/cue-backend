"""Item 3: app/capture/pipeline.py — the full adapter -> normalise ->
identity-resolve -> consent-gate -> extract chain, run against the
FixtureAdapter (real, running against cue-eval/cases.json, not mocked) with a
scripted fake LLM client (fast, deterministic, no live Ollama required — same
posture tests/test_extractor.py's own FakeModelClient establishes).
"""

import json
from datetime import datetime, timezone as dt_timezone

import pytest
from sqlalchemy import select

from app.capture.adapters.fixtures_adapter import FixtureAdapter
from app.capture.consent import upsert_consent_record
from app.capture.models import Message
from app.capture.pipeline import ingest_channel_backlog, ingest_raw_message
from app.capture.schema import RawCapturedMessage, compute_payload_hash
from app.models import Channel, Commitment, Evidence, Project
from tests.conftest import set_org_context


class ScriptedModelClient:
    """Returns a canned response keyed by a substring of the prompt (which
    always contains the source message verbatim, per
    app/ledger/extractor.py's build_prompt) — unlike a single fixed
    response, this lets one client instance answer several different real
    fixture cases differently within one test."""

    def __init__(self, rules: list[tuple[str, dict]], default: dict | None = None):
        self.rules = rules
        self.default = default or {"commitments": []}
        self.calls = 0

    async def complete(self, prompt: str, schema: dict) -> str:
        self.calls += 1
        for substring, response in self.rules:
            if substring in prompt:
                return json.dumps(response)
        return json.dumps(self.default)


def _commitment_response(evidence_span: str, deliverable_en: str) -> dict:
    return {
        "commitments": [
            {
                "act_type": "renegotiate",
                "deliverable_en": deliverable_en,
                "deliverable_original": deliverable_en,
                "evidence_span": evidence_span,
                "confidence": 0.9,
            }
        ]
    }


@pytest.mark.asyncio
async def test_ingest_channel_backlog_extracts_real_commitments(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    channel = Channel(project_id=project_id, type="whatsapp", external_ref="g1", healthy=True)
    app_session.add(channel)
    await app_session.commit()

    client = ScriptedModelClient(
        rules=[("screen install will be delayed 2 hrs", _commitment_response(
            "screen install will be delayed 2 hrs", "screen install"
        ))],
    )

    summary = await ingest_channel_backlog(
        app_session, project=project, channel=channel, adapter=FixtureAdapter(), client=client,
    )

    assert summary.received == 6  # every whatsapp case in cue-eval/cases.json
    assert summary.new_messages == 6
    assert summary.duplicates == 0
    assert summary.opted_out == 0
    assert summary.commitments_created == 1  # only T01's message matches the scripted rule

    messages = (
        await app_session.execute(select(Message).where(Message.channel_id == channel.id))
    ).scalars().all()
    assert len(messages) == 6
    assert all(m.extraction_attempted_at is not None for m in messages)
    assert all(m.language in ("en", "zh", "zh+en") for m in messages)

    commitments = (
        await app_session.execute(select(Commitment).where(Commitment.project_id == project_id))
    ).scalars().all()
    assert len(commitments) == 1
    assert commitments[0].deliverable_en == "screen install"

    # Item 1's whole point — Evidence.message_id is a real FK now, and this
    # is what actually populates it for a real-capture commitment (extract_case
    # itself, untouched, never sets it).
    evidence = (
        await app_session.execute(select(Evidence).where(Evidence.commitment_id == commitments[0].id))
    ).scalar_one()
    matching_message = next(m for m in messages if m.text and "screen install" in m.text)
    assert evidence.message_id == matching_message.id


@pytest.mark.asyncio
async def test_backlog_ingestion_is_idempotent_across_two_runs(app_session, org_and_project):
    """arq's at-least-once redelivery could run the same channel's backlog
    fetch twice — the second run must not create duplicate Message rows or
    duplicate Commitments."""
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    channel = Channel(project_id=project_id, type="whatsapp", external_ref="g1", healthy=True)
    app_session.add(channel)
    await app_session.commit()

    client = ScriptedModelClient(
        rules=[("screen install will be delayed 2 hrs", _commitment_response(
            "screen install will be delayed 2 hrs", "screen install"
        ))],
    )

    first = await ingest_channel_backlog(
        app_session, project=project, channel=channel, adapter=FixtureAdapter(), client=client,
    )
    second = await ingest_channel_backlog(
        app_session, project=project, channel=channel, adapter=FixtureAdapter(), client=client,
    )

    assert first.new_messages == 6
    assert second.new_messages == 0
    assert second.duplicates == 6
    assert second.commitments_created == 0  # no re-extraction on the duplicate pass

    messages = (
        await app_session.execute(select(Message).where(Message.channel_id == channel.id))
    ).scalars().all()
    assert len(messages) == 6
    commitments = (
        await app_session.execute(select(Commitment).where(Commitment.project_id == project_id))
    ).scalars().all()
    assert len(commitments) == 1


@pytest.mark.asyncio
async def test_opted_out_party_messages_never_reach_extraction(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    channel = Channel(project_id=project_id, type="whatsapp", external_ref="g1", healthy=True)
    app_session.add(channel)
    await app_session.commit()

    # T01's sender is "Ah Seng Production" — establish the identity, then opt them out.
    raw = RawCapturedMessage(
        external_id="T01",
        sender_external_id="Ah Seng Production",
        sent_at=datetime(2026, 6, 22, tzinfo=dt_timezone.utc),
        text="Morning Mei — screen install will be delayed 2 hrs, we'll start 4pm instead of 2pm.",
        raw_payload_hash=compute_payload_hash("whatsapp", "T01", b"seed"),
    )
    message, is_new, created, _media_processed = await ingest_raw_message(
        app_session, project=project, channel=channel, adapter=FixtureAdapter(), raw=raw,
        client=ScriptedModelClient(rules=[]),
    )
    await app_session.commit()
    assert message is not None

    await upsert_consent_record(
        app_session, party_id=message.author_party_id, project_id=project_id, status="opted_out",
    )
    await app_session.commit()

    client = ScriptedModelClient(rules=[])
    summary = await ingest_channel_backlog(
        app_session, project=project, channel=channel, adapter=FixtureAdapter(), client=client,
    )

    # The seeded T01 row (written *before* the opt-out took effect) is
    # untouched — opting out stops *future* capture, it doesn't retroactively
    # redact prior content (see app/capture/consent.py's is_opted_out
    # docstring for why that's a separate, out-of-scope concern). But T05,
    # the other real fixture case from this same now-opted-out sender, must
    # never have landed as a Message row via the backlog run.
    ah_seng_messages = (
        await app_session.execute(
            select(Message).where(
                Message.channel_id == channel.id, Message.sender_external_id == "Ah Seng Production"
            )
        )
    ).scalars().all()
    assert [m.external_id for m in ah_seng_messages] == ["T01"]
    assert summary.opted_out >= 1
