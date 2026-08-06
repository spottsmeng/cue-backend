"""FR-LCY-01 (PRD §6.5). Two layers, tested separately on purpose:

- app/ledger/lifecycle.py's validate_transition — pure Python, no DB needed.
- the BEFORE UPDATE trigger added by migration d05dce0f415d — proven by
  mutating Commitment.state directly via the ORM and committing, *skipping*
  validate_transition entirely. If only the API-level 409 were tested, a
  future direct-write caller (a script, a different service) could still
  corrupt the state machine with the DB none the wiser; this is what proves
  that isn't true. Same "verified in code, not trusted" posture the evidence
  trigger gets in test_evidence_constraint.py.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from app.ledger.extractor import _get_commitment_act_term
from app.ledger.lifecycle import TRANSITIONS, InvalidTransition, validate_transition
from app.models import Commitment, Evidence
from tests.conftest import set_org_context


def test_every_documented_transition_is_accepted():
    for from_state, allowed in TRANSITIONS.items():
        for to_state in allowed:
            validate_transition(from_state, to_state)  # must not raise


@pytest.mark.parametrize(
    "from_state,to_state",
    [
        ("proposed", "delivered"),  # must go through committed or at_risk first
        ("proposed", "at_risk"),
        ("committed", "broken"),  # must pass through at_risk
        ("delivered", "committed"),  # delivered is terminal
        ("withdrawn", "committed"),  # withdrawn is terminal
        ("broken", "delivered"),  # broken can only go to renegotiated
    ],
)
def test_undocumented_transitions_are_rejected(from_state, to_state):
    with pytest.raises(InvalidTransition, match=f"{from_state!r} -> {to_state!r}"):
        validate_transition(from_state, to_state)


async def _make_commitment(app_session, project_id, vendor, internal, state: str) -> Commitment:
    act_term = await _get_commitment_act_term(app_session, "confirm")
    commitment = Commitment(
        project_id=project_id,
        party_id=vendor.id,
        counterparty_id=internal.id,
        act_type_id=act_term.id,
        state=state,
        deliverable_en="LED screen install",
        deliverable_original="LED screen install",
        confidence=0.9,
        field_confidence={},
        verification_state="auto",
    )
    app_session.add(commitment)
    await app_session.flush()
    app_session.add(
        Evidence(
            commitment_id=commitment.id,
            channel="whatsapp",
            sent_at=datetime.now(timezone.utc),
            language="en",
            original_text="screens will be up by 2pm",
            span_start=0,
            span_end=10,
        )
    )
    await app_session.commit()
    return commitment


@pytest.mark.asyncio
async def test_db_trigger_allows_a_valid_transition(app_session, org_and_project, parties):
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)
    commitment = await _make_commitment(app_session, project_id, vendor, internal, "proposed")

    commitment.state = "committed"  # valid; not going through validate_transition at all
    await app_session.commit()  # must not raise

    refreshed = (
        await app_session.execute(select(Commitment).where(Commitment.id == commitment.id))
    ).scalar_one()
    assert refreshed.state == "committed"


@pytest.mark.asyncio
async def test_db_trigger_rejects_an_invalid_transition_even_bypassing_the_app_layer(
    app_session, org_and_project, parties
):
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)
    commitment = await _make_commitment(app_session, project_id, vendor, internal, "proposed")
    commitment_id = commitment.id  # captured before rollback expires the instance

    commitment.state = "delivered"  # invalid: proposed -> delivered directly (FR-LCY-01)
    with pytest.raises(DBAPIError, match="invalid commitment state transition"):
        await app_session.commit()
    await app_session.rollback()

    refreshed = (
        await app_session.execute(select(Commitment).where(Commitment.id == commitment_id))
    ).scalar_one()
    assert refreshed.state == "proposed"


@pytest.mark.asyncio
async def test_db_trigger_does_not_fire_when_state_is_unchanged(
    app_session, org_and_project, parties
):
    """The trigger is scoped to `WHEN (NEW.state IS DISTINCT FROM OLD.state)`
    — an update that leaves state alone must not be blocked by it."""
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)
    commitment = await _make_commitment(app_session, project_id, vendor, internal, "proposed")

    commitment.deliverable_en = "LED screen install (revised)"
    await app_session.commit()  # must not raise

    refreshed = (
        await app_session.execute(select(Commitment).where(Commitment.id == commitment.id))
    ).scalar_one()
    assert refreshed.deliverable_en == "LED screen install (revised)"
