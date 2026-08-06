"""Codifies the timezone-naive-column bug caught during this build: every
hand-declared datetime column defaulted to SQLAlchemy's plain DateTime (no
timezone) unless told otherwise via TZDateTime, and the fixture cases carry
explicit +08:00 offsets that would have been silently dropped or
misinterpreted. Checked by hand via information_schema.columns at the time;
this proves the actual round-trip behaviour, not just the column type.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.ledger.extractor import _get_commitment_act_term, _parse_timestamp
from app.models import Commitment, Evidence
from tests.conftest import set_org_context


def test_parse_timestamp_preserves_offset():
    """The exact value shape cue-eval/cases.json T01 produces."""
    dt = _parse_timestamp("2026-06-22T16:00:00+08:00")
    assert dt.utcoffset() == timedelta(hours=8)
    assert dt.astimezone(timezone.utc) == datetime(2026, 6, 22, 8, 0, tzinfo=timezone.utc)


def test_parse_timestamp_date_only_gets_a_zone():
    """T08's due_at ("2026-06-19") has no time component at all — must not
    come out naive, since due_at is timestamptz."""
    dt = _parse_timestamp("2026-06-19")
    assert dt.tzinfo is not None


@pytest.mark.asyncio
async def test_due_at_round_trips_through_postgres(
    app_session, owner_session, org_and_project, parties
):
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)

    # Deliberately a non-UTC, non-+08:00 offset, so a silent "assume Singapore
    # time" bug elsewhere in the stack couldn't accidentally make this pass.
    due_at = datetime(2026, 6, 22, 16, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))

    act_term = await _get_commitment_act_term(app_session, "confirm")
    commitment = Commitment(
        project_id=project_id,
        party_id=vendor.id,
        counterparty_id=internal.id,
        act_type_id=act_term.id,
        state="proposed",
        deliverable_en="Test",
        deliverable_original="Test",
        due_at=due_at,
        confidence=0.9,
        field_confidence={},
        verification_state="pending_verification",
    )
    app_session.add(commitment)
    await app_session.flush()
    app_session.add(
        Evidence(
            commitment_id=commitment.id,
            channel="whatsapp",
            sent_at=datetime.now(timezone.utc),
            language="en",
            original_text="test",
            span_start=0,
            span_end=4,
        )
    )
    await app_session.commit()

    # A separate session (owner_session), not app_session re-queried after
    # expire_all() — proves what Postgres actually stored, and avoids a
    # MissingGreenlet this test hit under the commit -> expire_all -> execute
    # sequence on the same session.
    reloaded = (
        await owner_session.execute(select(Commitment).where(Commitment.id == commitment.id))
    ).scalar_one()

    assert reloaded.due_at.tzinfo is not None
    assert reloaded.due_at == due_at  # same instant, regardless of which offset it's displayed in
