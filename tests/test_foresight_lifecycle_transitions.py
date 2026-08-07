"""app/ledger/lifecycle.py's apply_automatic_transition — the FR-LCY-02/03
execution primitive every Foresight detector calls, exercised directly
end to end (validator + audit + Twin recompute), independent of any one
detector's own decision logic (those are covered in
tests/test_foresight_silence.py / test_foresight_forecast.py).
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.ledger.extractor import _get_commitment_act_term
from app.ledger.lifecycle import InvalidTransition, apply_automatic_transition
from app.models import AuditLog, Commitment, Evidence
from tests.conftest import set_org_context


async def _make_commitment(app_session, project_id, vendor_id, internal_id, state="committed") -> Commitment:
    act_term = await _get_commitment_act_term(app_session, "commit")
    commitment = Commitment(
        project_id=project_id, party_id=vendor_id, counterparty_id=internal_id, act_type_id=act_term.id,
        state=state, deliverable_en="LED screen install", confidence=1.0, verification_state="human_verified",
    )
    app_session.add(commitment)
    await app_session.flush()
    app_session.add(
        Evidence(
            commitment_id=commitment.id, channel="whatsapp", sent_at=datetime.now(timezone.utc),
            language="en", original_text="confirmed",
        )
    )
    await app_session.commit()
    return commitment


@pytest.mark.asyncio
async def test_apply_automatic_transition_end_to_end(app_session, org_and_project, parties):
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)
    commitment = await _make_commitment(app_session, project_id, vendor.id, internal.id, state="committed")

    await apply_automatic_transition(
        app_session, project_id=project_id, commitment=commitment, to_state="at_risk",
        trigger="silence", detail={"gap_days": 6.0},
    )
    await app_session.commit()

    await app_session.refresh(commitment)
    assert commitment.state == "at_risk"

    audit = (
        await app_session.execute(
            select(AuditLog).where(AuditLog.commitment_id == commitment.id, AuditLog.action == "state_transition")
        )
    ).scalar_one()
    assert audit.actor_id is None
    assert audit.from_state == "committed"
    assert audit.to_state == "at_risk"
    assert audit.detail == {"trigger": "silence", "gap_days": 6.0}


@pytest.mark.asyncio
async def test_apply_automatic_transition_rejects_a_structurally_invalid_transition(
    app_session, org_and_project, parties
):
    """A detector bug (e.g. trying delivered -> at_risk) must fail loudly,
    the same InvalidTransition a manual API call would get — never silently
    coerced."""
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)
    commitment = await _make_commitment(app_session, project_id, vendor.id, internal.id, state="delivered")

    with pytest.raises(InvalidTransition):
        await apply_automatic_transition(
            app_session, project_id=project_id, commitment=commitment, to_state="at_risk",
            trigger="silence",
        )
    await app_session.rollback()

    await app_session.refresh(commitment)
    assert commitment.state == "delivered"  # unchanged
