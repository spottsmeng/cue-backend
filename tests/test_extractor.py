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
    extract_case,
)
from app.models import Commitment, Evidence
from sqlalchemy import select
from tests.conftest import set_org_context

CONTEXT = {
    "project": "Test Project",
    "client": "Test Client",
    "timezone": "Asia/Singapore",
    "venue": "Test Venue",
    "build_up": ["2026-06-22"],
    "event_days": ["2026-06-24"],
    "doors": "2026-06-24T09:00:00+08:00",
    "known_milestones": [{"name": "Test Milestone", "due": "2026-06-20"}],
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

    async def complete(self, prompt: str, schema: dict) -> str:
        self.calls.append((prompt, schema))
        return json.dumps(self.response)


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
