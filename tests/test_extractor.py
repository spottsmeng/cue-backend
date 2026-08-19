"""The extractor's own logic, using a fake LLM client — fast, deterministic,
no live Ollama required. What's under test here is everything *around* the
model call: code-level evidence-span verification (CLAUDE.md: 'verified in
code, not trusted' — never actually exercised by hand this session, only the
DB-level trigger was), FR-LED-07's verification_state routing, and the write
path itself. The live-Ollama path is covered separately by
scripts/extract_fixtures.py, which is not part of this automated suite on
purpose — it depends on an external local service and isn't reproducible in
CI.
"""

import json

import pytest

from app.ledger.extractor import (
    RejectedExtraction,
    _get_commitment_act_term,
    _get_or_create_party,
    extract_case,
)
from app.models import Commitment, Evidence, Party
from sqlalchemy import select
from tests.conftest import FAKE_LLM_USAGE, set_org_context

CONTEXT = {
    "project": "Test Project",
    "client": "Test Client",
    "timezone": "Asia/Singapore",
    "venue": "Test Venue",
    "build_up": ["2026-06-22"],
    "event_days": ["2026-06-24"],
    "doors": "2026-06-24T09:00:00+08:00",
    "known_milestones": [{"name": "Test Milestone", "due": "2026-06-20"}],
    "vendors": [{"party": "Ah Seng Production", "category": "AV"}],
}


def make_case(message: str, **overrides) -> dict:
    case = {
        "id": "TX",
        "band": "test",
        "lang": "en",
        "channel": "whatsapp",
        "party": "Test Vendor",
        "sent_at": "2026-06-22T09:00:00+08:00",
        "message": message,
        "sent_weekday": None,
    }
    case.update(overrides)
    return case


class FakeModelClient:
    """Ignores the prompt entirely — returns whatever canned response the test
    configured, so extractor logic can be tested independent of any real
    model's behaviour."""

    def __init__(self, response: dict):
        self.response = response
        self.calls: list[tuple[str, dict]] = []

    async def complete(self, prompt: str, schema: dict):
        self.calls.append((prompt, schema))
        return json.dumps(self.response), FAKE_LLM_USAGE


@pytest.mark.asyncio
async def test_evidence_span_not_in_message_is_rejected(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    case = make_case("screen install will be delayed 2 hrs")
    fake = FakeModelClient(
        {
            "commitments": [
                {
                    "act_type": "renegotiate",
                    "deliverable_en": "screen install",
                    "deliverable_original": "screen install",
                    "evidence_span": "this text does not appear in the message anywhere",
                    "confidence": 0.9,
                }
            ]
        }
    )

    with pytest.raises(RejectedExtraction, match="evidence_span not found verbatim"):
        await extract_case(
            app_session,
            project_id=project_id,
            organisation_id=org_id,
            context=CONTEXT,
            case=case,
            client=fake,
        )

    # Nothing should have been left dangling in the session.
    await app_session.rollback()
    count = (await app_session.execute(select(Commitment))).scalars().all()
    assert count == []


@pytest.mark.asyncio
async def test_valid_extraction_writes_commitment_and_evidence(
    app_session, owner_session, org_and_project
):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    case = make_case("screen install will be delayed 2 hrs, confirmed.")
    fake = FakeModelClient(
        {
            "commitments": [
                {
                    "act_type": "confirm",
                    "deliverable_en": "screen install",
                    "deliverable_original": "screen install",
                    "evidence_span": "screen install will be delayed 2 hrs",
                    "confidence": 0.85,
                }
            ]
        }
    )

    created = await extract_case(
        app_session,
        project_id=project_id,
        organisation_id=org_id,
        context=CONTEXT,
        case=case,
        client=fake,
    )
    await app_session.commit()
    assert len(created) == 1

    # A genuinely separate session/connection (owner_session), not the same
    # app_session re-queried after expire_all() — proves what Postgres
    # actually persisted rather than what's cached in this process's identity
    # map, and does it without app_session's commit -> expire_all -> execute
    # sequence, which triggers a MissingGreenlet under this test setup.
    commitment = (
        await owner_session.execute(select(Commitment).where(Commitment.id == created[0].id))
    ).scalar_one()
    assert commitment.deliverable_en == "screen install"
    assert commitment.confidence == 0.85
    assert commitment.state == "proposed"

    evidence = (
        await owner_session.execute(
            select(Evidence).where(Evidence.commitment_id == commitment.id)
        )
    ).scalar_one()
    assert evidence.original_text == case["message"]
    assert (
        case["message"][evidence.span_start : evidence.span_end]
        == "screen install will be delayed 2 hrs"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra_fields,expected_state",
    [
        ({"amount": 8400, "currency": "SGD"}, "pending_verification"),
        ({"due_at": "2026-06-22T16:00:00+08:00"}, "pending_verification"),
        ({}, "auto"),
    ],
)
async def test_verification_state_routing(
    app_session, org_and_project, extra_fields, expected_state
):
    """FR-LED-07: price, approval, date and scope-change fields route to
    pending_verification regardless of confidence."""
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    case = make_case("truss quote is 8400 sgd, confirmed for tomorrow")
    response = {
        "act_type": "quote",
        "deliverable_en": "truss",
        "deliverable_original": "truss",
        "evidence_span": "truss quote is 8400 sgd",
        "confidence": 0.95,
        **extra_fields,
    }
    fake = FakeModelClient({"commitments": [response]})

    created = await extract_case(
        app_session,
        project_id=project_id,
        organisation_id=org_id,
        context=CONTEXT,
        case=case,
        client=fake,
    )
    await app_session.commit()

    assert created[0].verification_state == expected_state


@pytest.mark.asyncio
async def test_unseeded_commitment_act_raises_lookup_error(app_session):
    """Not reachable through the full extract_case path today, since every
    act_type Pydantic's Literal permits is seeded by the migration — this
    tests _get_commitment_act_term directly as a defensive check on the
    lookup itself, in case that invariant (seed matches Literal) ever
    drifts silently."""
    with pytest.raises(LookupError, match="not seeded"):
        await _get_commitment_act_term(app_session, "definitely_not_a_real_code")


# --- vendor-attribution-task.md ---------------------------------------------


@pytest.mark.asyncio
async def test_get_or_create_party_is_type_scoped(app_session, org_and_project):
    """Fix 1: a same-named row of a *different* type must never be returned
    — this is the exact chain the real bug rode in on (a person-type Party
    app/capture/identity.py already created for a channel identity, silently
    reused as the vendor_org party because the old lookup never filtered on
    type)."""
    org_id, _ = org_and_project
    await set_org_context(app_session, org_id)

    person = Party(organisation_id=org_id, display_name="Golden Sound & Light", type="person")
    app_session.add(person)
    await app_session.flush()

    vendor = await _get_or_create_party(app_session, org_id, "Golden Sound & Light", "vendor_org")
    assert vendor.id != person.id
    assert vendor.type == "vendor_org"

    # idempotent: a second call with the same (name, type) finds the row it
    # just created, not a third one.
    again = await _get_or_create_party(app_session, org_id, "Golden Sound & Light", "vendor_org")
    assert again.id == vendor.id


def _team_collaboration_case(message: str, **overrides) -> dict:
    case = make_case(message, channel="mattermost", party="Farah Rahman")
    case["channel_capability"] = "team_collaboration"
    case.update(overrides)
    return case


@pytest.mark.asyncio
async def test_internal_channel_commitment_never_attributed_to_the_sender(
    app_session, org_and_project
):
    """The bug, reproduced directly: on a team_collaboration channel, the
    message's author (Farah Rahman) must never become the commitment's
    vendor party — regardless of what the model returns for
    counterparty_name."""
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    case = _team_collaboration_case(
        "While we're on Golden Sound & Light — can someone confirm the Stage Power "
        "Distribution Board ($6,200) is still tracking for the 29th?"
    )
    fake = FakeModelClient(
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
        }
    )

    created = await extract_case(
        app_session, project_id=project_id, organisation_id=org_id,
        context=CONTEXT, case=case, client=fake,
    )
    await app_session.commit()

    party = (await app_session.execute(select(Party).where(Party.id == created[0].party_id))).scalar_one()
    assert party.display_name != "Farah Rahman"
    assert party.display_name == "Golden Sound & Light"
    assert party.type == "vendor_org"


@pytest.mark.asyncio
async def test_internal_channel_matches_known_project_vendor(app_session, org_and_project):
    """When the model's counterparty_name matches a vendor already known to
    the project (context["vendors"]), that's a confident attribution — no
    forced pending_verification purely from party confidence."""
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    case = _team_collaboration_case(
        "Heads up — ah seng production just confirmed the truss frame is ready.",
        party="Marcus Lim",
    )
    fake = FakeModelClient(
        {
            "commitments": [
                {
                    "act_type": "confirm",
                    "deliverable_en": "truss frame",
                    "deliverable_original": "truss frame",
                    "counterparty_name": "ah seng production",  # model's own casing
                    "evidence_span": "truss frame is ready",
                    "confidence": 0.9,
                }
            ]
        }
    )

    created = await extract_case(
        app_session, project_id=project_id, organisation_id=org_id,
        context=CONTEXT, case=case, client=fake,
    )
    await app_session.commit()

    assert created[0].verification_state == "auto"
    party = (await app_session.execute(select(Party).where(Party.id == created[0].party_id))).scalar_one()
    # Canonical stored name from context["vendors"], not the model's raw casing.
    assert party.display_name == "Ah Seng Production"


@pytest.mark.asyncio
async def test_internal_channel_with_no_named_vendor_lands_unresolved_and_pending(
    app_session, org_and_project
):
    """No counterparty_name at all (model couldn't identify one) — fix 3:
    route to pending_verification rather than silently attaching to the
    internal sender."""
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    case = _team_collaboration_case("Can someone chase the vendor on this?")
    fake = FakeModelClient(
        {
            "commitments": [
                {
                    "act_type": "query",
                    "deliverable_en": "vendor follow-up",
                    "deliverable_original": "vendor follow-up",
                    "evidence_span": "chase the vendor on this",
                    "confidence": 0.5,
                }
            ]
        }
    )

    created = await extract_case(
        app_session, project_id=project_id, organisation_id=org_id,
        context=CONTEXT, case=case, client=fake,
    )
    await app_session.commit()

    assert created[0].verification_state == "pending_verification"
    party = (await app_session.execute(select(Party).where(Party.id == created[0].party_id))).scalar_one()
    assert party.display_name == "Unresolved Vendor"
    assert party.type == "vendor_org"


@pytest.mark.asyncio
async def test_external_vendor_chat_behaviour_is_unchanged(app_session, org_and_project):
    """No channel_capability at all (the default for every pre-existing
    caller/test, and for a real whatsapp/wechat channel) — the sender is
    still the vendor, exactly as before this task."""
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    case = make_case("screen install will be delayed 2 hrs, confirmed.", party="Ah Seng Production")
    fake = FakeModelClient(
        {
            "commitments": [
                {
                    "act_type": "confirm",
                    "deliverable_en": "screen install",
                    "deliverable_original": "screen install",
                    "evidence_span": "screen install will be delayed 2 hrs",
                    "confidence": 0.85,
                }
            ]
        }
    )

    created = await extract_case(
        app_session, project_id=project_id, organisation_id=org_id,
        context=CONTEXT, case=case, client=fake,
    )
    await app_session.commit()

    assert created[0].verification_state == "auto"
    party = (await app_session.execute(select(Party).where(Party.id == created[0].party_id))).scalar_one()
    assert party.display_name == "Ah Seng Production"
