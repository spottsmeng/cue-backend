"""Codifies the timezone-naive-column bug caught during this build: every
hand-declared datetime column defaulted to SQLAlchemy's plain DateTime (no
timezone) unless told otherwise via TZDateTime, and the fixture cases carry
explicit +08:00 offsets that would have been silently dropped or
misinterpreted. Checked by hand via information_schema.columns at the time;
this proves the actual round-trip behaviour, not just the column type.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.ledger.extractor import _get_commitment_act_term, _parse_timestamp, _project_tzinfo
from app.models import Commitment, Evidence
from tests.conftest import set_org_context


_SG = ZoneInfo("Asia/Singapore")


def test_parse_timestamp_preserves_offset():
    """The exact value shape cue-eval/cases.json T01 produces. An explicit
    offset in the value always wins over the project's zone."""
    dt = _parse_timestamp("2026-06-22T16:00:00+08:00", _SG)
    assert dt.utcoffset() == timedelta(hours=8)
    assert dt.astimezone(timezone.utc) == datetime(2026, 6, 22, 8, 0, tzinfo=timezone.utc)


def test_parse_timestamp_date_only_gets_a_zone():
    """T08's due_at ("2026-06-19") has no time component at all — must not
    come out naive, since due_at is timestamptz."""
    dt = _parse_timestamp("2026-06-19", _SG)
    assert dt.tzinfo is not None


def test_naive_timestamp_uses_the_projects_own_zone_not_a_fixed_plus_8():
    """The tenant-coupling half of the same bug: a naive value used to be read
    as +08:00 for every organisation, so a London tenant's due dates landed
    eight hours early — in a product whose whole value is due dates."""
    sg = _parse_timestamp("2026-06-22T16:00:00", _SG)
    london = _parse_timestamp("2026-06-22T16:00:00", ZoneInfo("Europe/London"))

    assert sg.utcoffset() == timedelta(hours=8)
    assert london.utcoffset() == timedelta(hours=1)  # BST in June
    assert london.astimezone(timezone.utc) - sg.astimezone(timezone.utc) == timedelta(hours=7)


def test_project_tzinfo_falls_back_rather_than_raising_on_a_bad_zone():
    """An unset or malformed Project.timezone must not take extraction down —
    NFR-AVL-02 says capture never loses a message."""
    assert _project_tzinfo({"timezone": "Asia/Singapore"}) == _SG
    assert _project_tzinfo({"timezone": "Not/AZone"}).utcoffset(None) == timedelta(hours=8)
    assert _project_tzinfo({}).utcoffset(None) == timedelta(hours=8)


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
