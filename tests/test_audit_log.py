"""FR-LED-12: an immutable, append-only log of every ledger mutation. Three
things get proven here, each unprovable by the other two: a mutation
actually writes a row (not just "the code path that would write it runs
without erroring"), the append-only trigger genuinely blocks UPDATE/DELETE
rather than the app simply choosing not to issue them, and RLS isolates
audit_log the same way it isolates commitments.
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.ledger.audit import record_audit_event
from app.ledger.extractor import _get_commitment_act_term
from app.models import AuditLog, Commitment, Evidence
from tests.conftest import FAKE_LLM_USAGE, set_org_context


async def _make_commitment(app_session, project_id, vendor, internal) -> Commitment:
    act_term = await _get_commitment_act_term(app_session, "confirm")
    commitment = Commitment(
        project_id=project_id,
        party_id=vendor.id,
        counterparty_id=internal.id,
        act_type_id=act_term.id,
        state="proposed",
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
async def test_record_audit_event_writes_a_row(
    app_session, owner_session, org_and_project, parties, seeded_user
):
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)
    commitment = await _make_commitment(app_session, project_id, vendor, internal)
    actor_id = seeded_user.id

    await record_audit_event(
        app_session,
        project_id=project_id,
        commitment_id=commitment.id,
        action="state_transition",
        actor_id=actor_id,
        from_state="proposed",
        to_state="committed",
        detail={"reason": "vendor confirmed"},
    )
    await app_session.commit()

    # A genuinely separate session (owner_session), same reasoning as
    # test_extractor.py: proves what Postgres actually persisted.
    row = (
        await owner_session.execute(
            select(AuditLog).where(AuditLog.commitment_id == commitment.id)
        )
    ).scalar_one()
    assert row.action == "state_transition"
    assert row.actor_id == actor_id
    assert row.from_state == "proposed"
    assert row.to_state == "committed"
    assert row.detail == {"reason": "vendor confirmed"}
    assert row.project_id == project_id


@pytest.mark.asyncio
async def test_audit_log_is_append_only(app_session, org_and_project, parties):
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)
    commitment = await _make_commitment(app_session, project_id, vendor, internal)

    entry = await record_audit_event(
        app_session,
        project_id=project_id,
        commitment_id=commitment.id,
        action="created",
        actor_id=None,
        to_state="proposed",
    )
    await app_session.commit()

    # A plain UPDATE fires the trigger immediately on execute, not deferred
    # to commit like an ORM attribute change would be.
    with pytest.raises(DBAPIError, match="append-only"):
        await app_session.execute(
            text("UPDATE audit_log SET detail = '{}'::jsonb WHERE id = :id"), {"id": entry.id}
        )
    await app_session.rollback()


@pytest.mark.asyncio
async def test_audit_log_rows_cannot_be_deleted(app_session, org_and_project, parties):
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)
    commitment = await _make_commitment(app_session, project_id, vendor, internal)

    entry = await record_audit_event(
        app_session,
        project_id=project_id,
        commitment_id=commitment.id,
        action="created",
        actor_id=None,
        to_state="proposed",
    )
    await app_session.commit()

    with pytest.raises(DBAPIError, match="append-only"):
        await app_session.execute(text("DELETE FROM audit_log WHERE id = :id"), {"id": entry.id})
    await app_session.rollback()


@pytest.mark.asyncio
async def test_audit_log_isolated_by_rls(app_session, org_and_project, parties):
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)
    commitment = await _make_commitment(app_session, project_id, vendor, internal)
    await record_audit_event(
        app_session,
        project_id=project_id,
        commitment_id=commitment.id,
        action="created",
        actor_id=None,
        to_state="proposed",
    )
    await app_session.commit()

    await set_org_context(app_session, uuid.uuid4())  # a different, unrelated org
    visible = (
        await app_session.execute(select(AuditLog).where(AuditLog.commitment_id == commitment.id))
    ).scalars().all()
    assert visible == []


@pytest.mark.asyncio
async def test_extraction_writes_an_audit_entry(app_session, org_and_project):
    """FR-LED-12 covers every ledger mutation, not only the REST-driven ones
    — extraction (app/ledger/extractor.py) writes one too."""
    import json

    from app.ledger.extractor import extract_case

    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)

    context = {
        "project": "Test Project",
        "client": "Test Client",
        "timezone": "Asia/Singapore",
        "venue": "Test Venue",
        "build_up": ["2026-06-22"],
        "event_days": ["2026-06-24"],
        "doors": "2026-06-24T09:00:00+08:00",
        "known_milestones": [{"name": "Test Milestone", "due": "2026-06-20"}],
    }
    case = {
        "id": "TX",
        "band": "test",
        "lang": "en",
        "channel": "whatsapp",
        "party": "Test Vendor",
        "sent_at": "2026-06-22T09:00:00+08:00",
        "message": "screen install will be delayed 2 hrs, confirmed.",
        "sent_weekday": None,
    }

    class FakeModelClient:
        async def complete(self, prompt: str, schema: dict):
            return (
                json.dumps(
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
                ),
                FAKE_LLM_USAGE,
            )

    created = await extract_case(
        app_session,
        project_id=project_id,
        organisation_id=org_id,
        context=context,
        case=case,
        client=FakeModelClient(),
    )
    await app_session.commit()

    row = (
        await app_session.execute(
            select(AuditLog).where(AuditLog.commitment_id == created[0].id)
        )
    ).scalar_one()
    assert row.action == "created"
    assert row.actor_id is None
    assert row.to_state == "proposed"
    assert row.evidence_id is not None
