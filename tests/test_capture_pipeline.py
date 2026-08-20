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
from app.models import Channel, Commitment, Evidence, Party, Project
from tests.conftest import FAKE_LLM_USAGE, set_org_context


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
        # Kept so a test can assert on what the pipeline actually built, not
        # only on what came back — the already-logged context block is a
        # property of the prompt, and asserting it here is what distinguishes
        # "the model was told" from "the model happened to guess right".
        self.seen_prompts: list[str] = []

    async def complete(self, prompt: str, schema: dict):
        self.calls += 1
        self.seen_prompts.append(prompt)
        for substring, response in self.rules:
            if substring in prompt:
                return json.dumps(response), FAKE_LLM_USAGE
        return json.dumps(self.default), FAKE_LLM_USAGE


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
async def test_internal_channel_message_attributes_to_named_vendor_not_sender(
    app_session, org_and_project
):
    """vendor-attribution-task.md, end-to-end through the real pipeline:
    channel.type -> channel_types.capability lookup
    (app/capture/pipeline.py's _channel_capability) -> build_case ->
    extract_case's vendor resolution. Confirms the real wiring, not just
    extract_case in isolation (tests/test_extractor.py already covers that
    function's own logic directly)."""
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    channel = Channel(project_id=project_id, type="mattermost", external_ref="c1", healthy=True)
    app_session.add(channel)
    await app_session.commit()

    raw = RawCapturedMessage(
        external_id="M01",
        sender_external_id="Farah Rahman",
        sent_at=datetime(2026, 6, 15, tzinfo=dt_timezone.utc),
        text=(
            "While we're on Golden Sound & Light — can someone confirm the Stage "
            "Power Distribution Board ($6,200) is still tracking for the 29th?"
        ),
        raw_payload_hash=compute_payload_hash("mattermost", "M01", b"seed"),
    )
    client = ScriptedModelClient(
        rules=[
            (
                "Stage Power Distribution Board",
                {
                    "commitments": [
                        {
                            "act_type": "query",
                            "deliverable_en": "Stage Power Distribution Board",
                            "deliverable_original": "Stage Power Distribution Board",
                            "amount": 6200,
                            "currency": "SGD",
                            "counterparty_name": "Golden Sound & Light",
                            "evidence_span": "Stage Power Distribution Board ($6,200)",
                            "confidence": 0.9,
                        }
                    ]
                },
            )
        ],
    )

    ingested = await ingest_raw_message(
        app_session, project=project, channel=channel, adapter=FixtureAdapter(), raw=raw, client=client,
    )
    message = ingested.message
    await app_session.commit()
    assert ingested.is_new
    assert ingested.extraction.created == 1

    commitment = (
        await app_session.execute(select(Commitment).where(Commitment.project_id == project_id))
    ).scalar_one()
    party = (
        await app_session.execute(select(Party).where(Party.id == commitment.party_id))
    ).scalar_one()
    assert party.display_name == "Golden Sound & Light"
    assert party.type == "vendor_org"

    sender_party = (
        await app_session.execute(select(Party).where(Party.id == message.author_party_id))
    ).scalar_one()
    assert sender_party.display_name == "Farah Rahman"
    assert commitment.party_id != sender_party.id


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
    ingested = await ingest_raw_message(
        app_session, project=project, channel=channel, adapter=FixtureAdapter(), raw=raw,
        client=ScriptedModelClient(rules=[]),
    )
    message = ingested.message
    await app_session.commit()
    assert ingested.message is not None

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


# --- over/under-splitting fixes (Over- and Under-splitting…pdf) --------------


@pytest.mark.asyncio
async def test_second_message_sees_the_first_ones_commitment_in_context(
    app_session, org_and_project
):
    """The end-to-end shape of the reported "AI invents fake promises out of
    people just talking": message 1 logs a real commitment, message 2 is a
    colleague chasing it. Before this, message 2 had no way to know message 1
    existed and produced a second, fake ledger row. Now the prompt carries the
    already-logged list and the model can point at it — asserted here on the
    *prompt* the pipeline actually built, not just on extractor internals.
    """
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (
        await app_session.execute(select(Project).where(Project.id == project_id))
    ).scalar_one()
    channel = Channel(project_id=project_id, type="mattermost", external_ref="town-square")
    app_session.add(channel)
    await app_session.flush()

    client = ScriptedModelClient(
        rules=[
            ("screen install will be delayed", _commitment_response(
                "screen install will be delayed", "screen install")),
            # The chaser links instead of creating: relates_to="C1".
            ("still lands before end of day", {
                "commitments": [{
                    "act_type": "query",
                    "deliverable_en": "screen install",
                    "deliverable_original": "screen install",
                    "evidence_span": "still lands before end of day",
                    "relates_to": "C1",
                    "confidence": 0.9,
                }]
            }),
        ],
    )

    async def ingest(external_id, text):
        raw = RawCapturedMessage(
            external_id=external_id, sender_external_id="Mei Tan",
            sent_at=datetime(2026, 6, 22, tzinfo=dt_timezone.utc), text=text,
            raw_payload_hash=compute_payload_hash("mattermost", external_id, text.encode()),
        )
        return await ingest_raw_message(
            app_session, project=project, channel=channel, adapter=FixtureAdapter(),
            raw=raw, client=client,
        )

    created1 = (await ingest("M1", "screen install will be delayed 2 hrs")).extraction.created
    await app_session.commit()
    assert created1 == 1

    second = await ingest(
        "M2", "Can we get written confirmation the 4pm start still lands before end of day?"
    )
    await app_session.commit()

    # The whole point: the second message created no new commitment.
    assert second.extraction.created == 0
    # ...but it was not a no-op either, and the counters have to tell those
    # apart. "created 0" alone reads identically for "nothing in this message"
    # and "correctly recognised as being about an existing commitment" — the
    # second being exactly how over-linking (under-splitting via the memory
    # path) would arrive, with created dropping and nothing else moving.
    assert second.extraction.linked == 1
    commitments = (
        await app_session.execute(select(Commitment).where(Commitment.project_id == project_id))
    ).scalars().all()
    assert len(commitments) == 1

    # ...and it did so because it was *told*, not because it guessed: the
    # second prompt carried the first commitment as C1.
    second_prompt = client.seen_prompts[-1]
    assert "ALREADY LOGGED" in second_prompt
    # The vendor is "Unresolved Vendor": this is a team_collaboration
    # channel, the sender is internal staff, and the model named no
    # counterparty — so app/ledger/extractor.py's own internal-channel path
    # parked it for human review rather than attaching it to Mei Tan. What
    # matters here is that the commitment is *in the list* under a stable ref.
    assert "C1 — Unresolved Vendor: screen install" in second_prompt

    # The chase is still recorded, as evidence on the commitment it was about.
    evidence = (
        await app_session.execute(
            select(Evidence).where(Evidence.commitment_id == commitments[0].id)
        )
    ).scalars().all()
    assert len(evidence) == 2


@pytest.mark.asyncio
async def test_a_rejected_extraction_never_loses_the_captured_message(
    app_session, org_and_project
):
    """NFR-AVL-02: capture must never lose a message. Extraction runs inside a
    SAVEPOINT so a failure under it rolls back the extraction only — the
    Message row was inserted in this same transaction and must survive."""
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (
        await app_session.execute(select(Project).where(Project.id == project_id))
    ).scalar_one()
    channel = Channel(project_id=project_id, type="mattermost", external_ref="town-square")
    app_session.add(channel)
    await app_session.flush()

    raw = RawCapturedMessage(
        external_id="BAD", sender_external_id="Mei Tan",
        sent_at=datetime(2026, 6, 22, tzinfo=dt_timezone.utc),
        text="screen install is on, and the truss is late",
        raw_payload_hash=compute_payload_hash("mattermost", "BAD", b"bad"),
    )
    client = ScriptedModelClient(
        rules=[("screen install is on", {"commitments": [
            {"act_type": "commit", "deliverable_en": "screen install",
             "deliverable_original": "screen install",
             "evidence_span": "screen install is on", "confidence": 0.9},
            {"act_type": "commit", "deliverable_en": "truss",
             "deliverable_original": "truss",
             "evidence_span": "a span that is not in this message", "confidence": 0.9},
        ]})],
    )

    ingested = await ingest_raw_message(
        app_session, project=project, channel=channel, adapter=FixtureAdapter(),
        raw=raw, client=client,
    )
    message = ingested.message
    await app_session.commit()

    assert ingested.is_new and ingested.extraction.created == 0
    # `rejected` used to be a declared-but-never-incremented field, so a run
    # reported zero rejections however many it actually had.
    assert ingested.extraction.rejected == 1
    # The message survived...
    assert (
        await app_session.execute(select(Message).where(Message.id == message.id))
    ).scalar_one_or_none() is not None
    # ...and the valid-looking first half of a rejected extraction did not
    # sneak onto the ledger with it.
    assert (
        await app_session.execute(select(Commitment).where(Commitment.project_id == project_id))
    ).scalars().all() == []


@pytest.mark.asyncio
async def test_linking_a_later_message_does_not_repoint_the_earlier_evidence(
    app_session, org_and_project
):
    """Evidence.message_id is backfilled per *evidence row*, not per
    commitment. When a second message cites an already-logged commitment, the
    commitment ends up with two evidence rows from two different messages —
    selecting by commitment_id (as this used to) would drag the first one's
    citation onto the second message, so the "show me where this came from"
    link would point at the wrong conversation.
    """
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (
        await app_session.execute(select(Project).where(Project.id == project_id))
    ).scalar_one()
    channel = Channel(project_id=project_id, type="mattermost", external_ref="town-square")
    app_session.add(channel)
    await app_session.flush()

    client = ScriptedModelClient(
        rules=[
            ("screen install will be delayed", _commitment_response(
                "screen install will be delayed", "screen install")),
            ("still lands before end of day", {"commitments": [{
                "act_type": "query", "deliverable_en": "screen install",
                "deliverable_original": "screen install",
                "evidence_span": "still lands before end of day",
                "relates_to": "C1", "confidence": 0.9,
            }]}),
        ],
    )

    async def ingest(external_id, text):
        raw = RawCapturedMessage(
            external_id=external_id, sender_external_id="Mei Tan",
            sent_at=datetime(2026, 6, 22, tzinfo=dt_timezone.utc), text=text,
            raw_payload_hash=compute_payload_hash("mattermost", external_id, text.encode()),
        )
        return await ingest_raw_message(
            app_session, project=project, channel=channel, adapter=FixtureAdapter(),
            raw=raw, client=client,
        )

    m1 = (await ingest("E1", "screen install will be delayed 2 hrs")).message
    await app_session.commit()
    m2 = (await ingest(
        "E2", "Can we get written confirmation the 4pm start still lands before end of day?"
    )).message
    await app_session.commit()

    commitment = (
        await app_session.execute(select(Commitment).where(Commitment.project_id == project_id))
    ).scalar_one()
    evidence = (
        await app_session.execute(
            select(Evidence).where(Evidence.commitment_id == commitment.id)
        )
    ).scalars().all()
    assert {e.message_id for e in evidence} == {m1.id, m2.id}
