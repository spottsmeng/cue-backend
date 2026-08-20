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

from app.ledger.context import load_open_commitment_context
from app.ledger.extractor import (
    REVIEW_UNAPPLIED_CLAIM,
    RejectedExtraction,
    _get_commitment_act_term,
    _get_or_create_party,
    extract_case,
)
from app.models import AuditLog, Commitment, Evidence, Party
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

    outcome = await extract_case(
        app_session,
        project_id=project_id,
        organisation_id=org_id,
        context=CONTEXT,
        case=case,
        client=fake,
    )
    await app_session.commit()
    assert len(outcome.created) == 1

    # A genuinely separate session/connection (owner_session), not the same
    # app_session re-queried after expire_all() — proves what Postgres
    # actually persisted rather than what's cached in this process's identity
    # map, and does it without app_session's commit -> expire_all -> execute
    # sequence, which triggers a MissingGreenlet under this test setup.
    commitment = (
        await owner_session.execute(select(Commitment).where(Commitment.id == outcome.created[0].id))
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

    outcome = await extract_case(
        app_session,
        project_id=project_id,
        organisation_id=org_id,
        context=CONTEXT,
        case=case,
        client=fake,
    )
    await app_session.commit()

    assert outcome.created[0].verification_state == expected_state


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

    outcome = await extract_case(
        app_session, project_id=project_id, organisation_id=org_id,
        context=CONTEXT, case=case, client=fake,
    )
    await app_session.commit()

    party = (await app_session.execute(select(Party).where(Party.id == outcome.created[0].party_id))).scalar_one()
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

    outcome = await extract_case(
        app_session, project_id=project_id, organisation_id=org_id,
        context=CONTEXT, case=case, client=fake,
    )
    await app_session.commit()

    assert outcome.created[0].verification_state == "auto"
    party = (await app_session.execute(select(Party).where(Party.id == outcome.created[0].party_id))).scalar_one()
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

    outcome = await extract_case(
        app_session, project_id=project_id, organisation_id=org_id,
        context=CONTEXT, case=case, client=fake,
    )
    await app_session.commit()

    assert outcome.created[0].verification_state == "pending_verification"
    party = (await app_session.execute(select(Party).where(Party.id == outcome.created[0].party_id))).scalar_one()
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

    outcome = await extract_case(
        app_session, project_id=project_id, organisation_id=org_id,
        context=CONTEXT, case=case, client=fake,
    )
    await app_session.commit()

    assert outcome.created[0].verification_state == "auto"
    party = (await app_session.execute(select(Party).where(Party.id == outcome.created[0].party_id))).scalar_one()
    assert party.display_name == "Ah Seng Production"


# --- over/under-splitting fixes (Over- and Under-splitting…pdf) --------------


def _item(**overrides) -> dict:
    item = {
        "act_type": "commit",
        "deliverable_en": "screen install",
        "deliverable_original": "screen install",
        "evidence_span": "screen install",
        "confidence": 0.95,
    }
    item.update(overrides)
    return item


@pytest.mark.asyncio
async def test_rejection_writes_nothing_even_without_a_rollback(app_session, org_and_project):
    """extract_case used to write as it looped and raise on the first bad
    evidence_span, so a message whose *second* item was invalid left the first
    one persisted. The caller that matters here (app/capture/pipeline.py)
    cannot simply roll back — the captured Message row is in the same
    transaction and NFR-AVL-02 says capture must never lose a message — so
    "the caller rolls back" was never an answer. Deliberately does NOT roll
    back, which is what makes this a regression test rather than a restatement
    of the older test above.
    """
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    case = make_case("screen install is on, and the truss is late")
    fake = FakeModelClient(
        {
            "commitments": [
                _item(),
                _item(deliverable_en="truss", evidence_span="this span is not in the message"),
            ]
        }
    )

    with pytest.raises(RejectedExtraction):
        await extract_case(
            app_session, project_id=project_id, organisation_id=org_id,
            context=CONTEXT, case=case, client=fake,
        )

    assert (await app_session.execute(select(Commitment))).scalars().all() == []
    assert (await app_session.execute(select(Evidence))).scalars().all() == []


@pytest.mark.asyncio
async def test_relates_to_attaches_evidence_instead_of_creating_a_commitment(
    app_session, org_and_project
):
    """The fix for the reported "AI invents fake promises out of people just
    talking": a message about a commitment already on the ledger adds a
    citation to it, and the ledger does not grow a second row."""
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)

    first = await extract_case(
        app_session, project_id=project_id, organisation_id=org_id, context=CONTEXT,
        case=make_case("screen install will start at 4pm"),
        client=FakeModelClient({"commitments": [_item(evidence_span="screen install")]}),
    )
    await app_session.flush()
    existing = first.created[0]

    ledger_context = await load_open_commitment_context(app_session, project_id=project_id)
    assert [i.ref for i in ledger_context] == ["C1"]

    outcome = await extract_case(
        app_session, project_id=project_id, organisation_id=org_id, context=CONTEXT,
        case=make_case("can someone confirm the screen install 4pm start still holds?"),
        client=FakeModelClient(
            {"commitments": [_item(act_type="query", evidence_span="screen install", relates_to="C1")]}
        ),
        ledger_context=ledger_context,
    )
    await app_session.commit()

    assert outcome.created == []
    assert outcome.linked == [existing.id]
    assert len((await app_session.execute(select(Commitment))).scalars().all()) == 1
    evidence = (
        await app_session.execute(select(Evidence).where(Evidence.commitment_id == existing.id))
    ).scalars().all()
    assert len(evidence) == 2  # the original citation, plus the message that chased it
    # A pure chase asserts nothing new, so it must not disturb the commitment.
    assert existing.verification_state == "auto"


@pytest.mark.asyncio
async def test_linked_message_carrying_a_price_flags_instead_of_dropping_it(
    app_session, org_and_project
):
    """The reported bug's silent inverse. A message that links to an existing
    commitment AND asserts a new price used to attach an Evidence row and
    discard the amount entirely — no field, no flag, no queue entry. The
    commitment is still never auto-edited (that is the whole point of the
    additive-only design), but the claim has to be *visible*.
    """
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)

    first = await extract_case(
        app_session, project_id=project_id, organisation_id=org_id, context=CONTEXT,
        case=make_case("screen install confirmed"),
        client=FakeModelClient({"commitments": [_item(evidence_span="screen install")]}),
    )
    await app_session.flush()
    existing = first.created[0]
    assert existing.amount is None
    assert existing.verification_state == "auto"

    ledger_context = await load_open_commitment_context(app_session, project_id=project_id)
    outcome = await extract_case(
        app_session, project_id=project_id, organisation_id=org_id, context=CONTEXT,
        case=make_case("the screen install is now 6200 not 5000"),
        client=FakeModelClient({"commitments": [_item(
            act_type="renegotiate", evidence_span="screen install",
            relates_to="C1", amount=6200, currency="SGD",
        )]}),
        ledger_context=ledger_context,
    )
    await app_session.commit()

    # Still additive: no new row, and the existing commitment is NOT rewritten.
    assert outcome.created == []
    assert outcome.linked == [existing.id]
    assert len((await app_session.execute(select(Commitment))).scalars().all()) == 1
    await app_session.refresh(existing)
    assert existing.amount is None

    # But it is now in front of a human, with the claim on the record.
    assert existing.verification_state == "pending_verification"
    audit = (
        await app_session.execute(
            select(AuditLog).where(AuditLog.commitment_id == existing.id)
        )
    ).scalars().all()
    linked_event = [a for a in audit if a.action == "evidence_added"][0]
    assert linked_event.detail["unapplied_claims"]["amount"] == 6200
    assert linked_event.detail["unapplied_claims"]["currency"] == "SGD"
    assert linked_event.detail["flagged_reason"] == REVIEW_UNAPPLIED_CLAIM
    assert REVIEW_UNAPPLIED_CLAIM in existing.verification_reasons
    assert linked_event.detail["prior_verification_state"] == "auto"


@pytest.mark.asyncio
async def test_low_confidence_link_is_flagged_like_a_low_confidence_creation(
    app_session, org_and_project
):
    """`_LOW_CONFIDENCE` guarded only the create path, so an unsure *link* —
    the model guessing that this message is about an existing commitment —
    was accepted in silence. The PDF's third requirement is that guessing
    wrong in either direction is worse than asking."""
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)

    first = await extract_case(
        app_session, project_id=project_id, organisation_id=org_id, context=CONTEXT,
        case=make_case("screen install confirmed"),
        client=FakeModelClient({"commitments": [_item(evidence_span="screen install")]}),
    )
    await app_session.flush()
    existing = first.created[0]

    ledger_context = await load_open_commitment_context(app_session, project_id=project_id)
    await extract_case(
        app_session, project_id=project_id, organisation_id=org_id, context=CONTEXT,
        case=make_case("is the screen install still on?"),
        client=FakeModelClient({"commitments": [_item(
            act_type="query", evidence_span="screen install", relates_to="C1", confidence=0.4,
        )]}),
        ledger_context=ledger_context,
    )
    await app_session.commit()

    await app_session.refresh(existing)
    assert existing.verification_state == "pending_verification"


@pytest.mark.asyncio
async def test_relates_to_naming_an_unoffered_commitment_is_rejected(
    app_session, org_and_project
):
    """The JSON Schema enum should make this undecodable, but a constraint the
    model was given is not one the database can rely on until code re-checks
    it — the same reason evidence_span is re-verified rather than trusted."""
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    with pytest.raises(RejectedExtraction, match="not offered to the model"):
        await extract_case(
            app_session, project_id=project_id, organisation_id=org_id, context=CONTEXT,
            case=make_case("screen install will start at 4pm"),
            client=FakeModelClient({"commitments": [_item(relates_to="C9")]}),
            ledger_context=[],
        )


@pytest.mark.asyncio
async def test_counterparty_echoing_the_sender_never_becomes_a_vendor(
    app_session, org_and_project
):
    """prompt.txt asks the model never to put the sender in counterparty_name.
    CLAUDE.md's first rule is that asking is not enforcement — without this,
    a model echoing the sender mints a vendor_org Party named after a Pico
    staff member, who then appears in the vendor directory and in vendor
    reliability metrics."""
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    case = make_case(
        "Marcus Lim here — screen install is still unconfirmed",
        party="Marcus Lim", channel="mattermost", channel_capability="team_collaboration",
    )
    outcome = await extract_case(
        app_session, project_id=project_id, organisation_id=org_id, context=CONTEXT, case=case,
        client=FakeModelClient({"commitments": [_item(counterparty_name="  marcus lim ")]}),
    )
    await app_session.commit()

    party = (
        await app_session.execute(select(Party).where(Party.id == outcome.created[0].party_id))
    ).scalar_one()
    assert party.display_name == "Unresolved Vendor"
    assert outcome.created[0].verification_state == "pending_verification"
    assert (
        await app_session.execute(select(Party).where(Party.display_name == "Marcus Lim"))
    ).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_overlapping_evidence_spans_route_both_to_human_review(
    app_session, org_and_project
):
    """Two commitments quoting the same stretch of text is the signature of
    one promise split in two. Not rejected — a message can legitimately
    interleave two promises — but never accepted silently either."""
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    case = make_case("screen install and rehearsal buffer are both tight")
    fake = FakeModelClient(
        {
            "commitments": [
                _item(evidence_span="screen install and rehearsal buffer"),
                _item(deliverable_en="rehearsal buffer", evidence_span="rehearsal buffer"),
            ]
        }
    )
    outcome = await extract_case(
        app_session, project_id=project_id, organisation_id=org_id,
        context=CONTEXT, case=case, client=fake,
    )
    await app_session.commit()

    assert len(outcome.created) == 2
    assert {c.verification_state for c in outcome.created} == {"pending_verification"}


@pytest.mark.asyncio
async def test_disjoint_evidence_spans_are_not_flagged(app_session, org_and_project):
    """The counterpart to the test above — a genuine multi-commitment message
    must not be dragged into human review just for having two commitments."""
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    case = make_case("screen install is on. Separately, truss supply is confirmed.")
    fake = FakeModelClient(
        {
            "commitments": [
                _item(evidence_span="screen install is on"),
                _item(deliverable_en="truss supply", evidence_span="truss supply is confirmed"),
            ]
        }
    )
    outcome = await extract_case(
        app_session, project_id=project_id, organisation_id=org_id,
        context=CONTEXT, case=case, client=fake,
    )
    await app_session.commit()
    assert {c.verification_state for c in outcome.created} == {"auto"}


@pytest.mark.asyncio
async def test_low_self_reported_confidence_routes_to_human_review(
    app_session, org_and_project
):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    outcome = await extract_case(
        app_session, project_id=project_id, organisation_id=org_id, context=CONTEXT,
        case=make_case("screen install maybe"),
        client=FakeModelClient({"commitments": [_item(confidence=0.4)]}),
    )
    await app_session.commit()
    assert outcome.created[0].verification_state == "pending_verification"


# --- CD03: the vendor is named in the span, not in counterparty_name -------


def test_vendor_prefix_matching_disambiguates_names_that_share_a_token():
    """Unit-level, because the risk is specific and this corpus contains it:
    "Ah Seng Production" and "Kim Seng Logistics" share the token "seng", so
    any-token matching would attribute one vendor's commitments to the other
    roughly at random. Leading-prefix matching asks for "ah seng" or "kim
    seng" and gets neither wrong."""
    from app.ledger.extractor import _vendors_named_in

    vendors = ["Ah Seng Production", "Kim Seng Logistics", "Vertex Fabrication", "Bloomworks"]

    assert _vendors_named_in("Ah Seng's LED screen install", vendors) == ["Ah Seng Production"]
    assert _vendors_named_in("Kim Seng will handle the lift", vendors) == ["Kim Seng Logistics"]
    assert _vendors_named_in("nothing relevant here", vendors) == []
    # A single distinctive token still counts; a two-letter one never does.
    assert _vendors_named_in("bloomworks confirmed", vendors) == ["Bloomworks"]
    assert _vendors_named_in("ahead of schedule", vendors) == []


@pytest.mark.asyncio
async def test_vendor_named_possessively_in_the_span_is_recovered_in_code(
    app_session, org_and_project
):
    """cue-eval CD03, reproduced: the model splits the two promises correctly
    (8/8 runs on qwen2.5:14b) and leaves counterparty_name empty on both,
    while writing the vendor into deliverable_original as "Ah Seng's LED
    screen install". The name was always there; nothing read it.

    Recovered in code rather than by prompt wording deliberately — CLAUDE.md
    records two attempts to teach the model this rule, both reverted for
    breaking IC01, one of them by burying the amount in a deliverable field.
    """
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    await _get_or_create_party(app_session, org_id, "Ah Seng Production", "vendor_org")
    await app_session.flush()

    case = _team_collaboration_case(
        "Ah Seng's LED screen install is still sitting as proposed, no confirmed date yet."
    )
    outcome = await extract_case(
        app_session, project_id=project_id, organisation_id=org_id, context=CONTEXT, case=case,
        client=FakeModelClient({"commitments": [_item(
            act_type="escalate", deliverable_en="LED screen install",
            deliverable_original="Ah Seng's LED screen install",
            evidence_span="Ah Seng's LED screen install", counterparty_name=None,
        )]}),
    )
    await app_session.commit()

    commitment = outcome.created[0]
    party = (
        await app_session.execute(select(Party).where(Party.id == commitment.party_id))
    ).scalar_one()
    assert party.display_name == "Ah Seng Production"  # not "Unresolved Vendor"
    # Still inference, so still a human's call — but on the right vendor.
    assert commitment.verification_state == "pending_verification"


@pytest.mark.asyncio
async def test_a_vendor_with_no_prior_commitment_is_still_recognised(
    app_session, org_and_project
):
    """`context["vendors"]` is built by joining Party to Commitment, so it
    only ever contains vendors who already hold one on this project. CD03's
    Vertex has none — which is exactly when getting attribution right matters
    most, and exactly the case that list cannot see. The lookup is org-scoped
    for this reason."""
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    await _get_or_create_party(app_session, org_id, "Vertex Fabrication", "vendor_org")
    await app_session.flush()
    assert not any(v["party"] == "Vertex Fabrication" for v in CONTEXT.get("vendors", []))

    case = _team_collaboration_case("Vertex's aluminium frame delivery has no confirmed date.")
    outcome = await extract_case(
        app_session, project_id=project_id, organisation_id=org_id, context=CONTEXT, case=case,
        client=FakeModelClient({"commitments": [_item(
            act_type="escalate", deliverable_en="aluminium frame delivery",
            deliverable_original="Vertex's aluminium frame delivery",
            evidence_span="Vertex's aluminium frame delivery", counterparty_name=None,
        )]}),
    )
    await app_session.commit()

    party = (
        await app_session.execute(
            select(Party).where(Party.id == outcome.created[0].party_id)
        )
    ).scalar_one()
    assert party.display_name == "Vertex Fabrication"


@pytest.mark.asyncio
async def test_one_commitment_naming_two_vendors_is_flagged_as_a_merge(
    app_session, org_and_project
):
    """The under-splitting detector — the mirror of _overlapping_span_indexes,
    which only ever caught over-splitting. A single commitment cannot be owed
    by two companies, so a span naming both is either two promises welded into
    one row (the PDF's Problem 2) or a span too wide to attribute. Neither is
    something to guess at, and neither was detectable before: a merged item is
    one item with one non-overlapping span, structurally invisible."""
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    await _get_or_create_party(app_session, org_id, "Ah Seng Production", "vendor_org")
    await _get_or_create_party(app_session, org_id, "Vertex Fabrication", "vendor_org")
    await app_session.flush()

    span = "Ah Seng's LED screen install and Vertex's aluminium frame delivery"
    case = _team_collaboration_case(f"{span} are both still unconfirmed.")
    outcome = await extract_case(
        app_session, project_id=project_id, organisation_id=org_id, context=CONTEXT, case=case,
        client=FakeModelClient({"commitments": [_item(
            act_type="escalate", deliverable_en="LED screen install and frame delivery",
            deliverable_original=span, evidence_span=span,
            # Deliberately a *confident* attribution: "Ah Seng Production" is
            # a known vendor, so without the merge detector this row would be
            # written straight to the ledger as `auto`. That is what makes
            # this test isolate the detector rather than re-testing the
            # unresolved-vendor path, which already flags everything it sees.
            counterparty_name="Ah Seng Production",
        )]}),
    )
    await app_session.commit()

    commitment = outcome.created[0]
    assert commitment.verification_state == "pending_verification"


@pytest.mark.asyncio
async def test_direct_vendor_chat_still_attributes_to_the_sender(
    app_session, org_and_project
):
    """The span scan must not leak onto external_vendor_chat. There, a vendor
    naming another vendor ("I'll coordinate the lift with Kim Seng") is
    describing a third party, not handing over the promise — attributing to
    the mentioned name would move commitments onto the wrong ledger."""
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    await _get_or_create_party(app_session, org_id, "Kim Seng Logistics", "vendor_org")
    await app_session.flush()

    outcome = await extract_case(
        app_session, project_id=project_id, organisation_id=org_id, context=CONTEXT,
        case=make_case("I'll coordinate the lift with Kim Seng on Tuesday"),
        client=FakeModelClient({"commitments": [_item(
            evidence_span="coordinate the lift with Kim Seng", counterparty_name=None,
        )]}),
    )
    await app_session.commit()

    party = (
        await app_session.execute(
            select(Party).where(Party.id == outcome.created[0].party_id)
        )
    ).scalar_one()
    assert party.display_name == "Test Vendor"  # the sender, per make_case
