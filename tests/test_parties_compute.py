"""app/parties/compute.py — FR-VRG-01's five metric computations, against a
hand-built commitment history (CLAUDE.md/Prompt 10's own testing
expectation: exact expected values, not just "a number came back").
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.foresight.deviation import create_manual_deviation
from app.ledger.extractor import _get_commitment_act_term
from app.models import Commitment, Evidence
from app.parties.compute import (
    compute_deviation_frequency,
    compute_median_response_time_days,
    compute_on_time_rate,
    compute_price_drift,
    compute_revision_churn,
)
from tests.conftest import set_org_context

NOW = datetime.now(timezone.utc)


async def _make_commitment(
    app_session,
    project_id,
    vendor_id,
    internal_id,
    *,
    state="committed",
    due_at=None,
    amount=None,
    supersedes=None,
    evidence_offsets_days: list[float] | None = None,
) -> Commitment:
    act_term = await _get_commitment_act_term(app_session, "commit")
    commitment = Commitment(
        project_id=project_id,
        party_id=vendor_id,
        counterparty_id=internal_id,
        act_type_id=act_term.id,
        state=state,
        deliverable_en="LED screen install",
        due_at=due_at,
        amount=amount,
        confidence=1.0,
        verification_state="human_verified",
        supersedes=supersedes or [],
    )
    app_session.add(commitment)
    await app_session.flush()
    # CLAUDE.md/FR-LED-03: every commitment needs at least one Evidence row
    # to satisfy the deferred constraint trigger at commit time.
    for offset in evidence_offsets_days or [1.0]:
        app_session.add(
            Evidence(
                commitment_id=commitment.id,
                channel="whatsapp",
                sent_at=NOW - timedelta(days=offset),
                language="en",
                original_text="ok, will confirm shortly",
            )
        )
    await app_session.flush()
    await app_session.commit()
    return commitment


@pytest.mark.asyncio
async def test_median_response_time_org_wide(app_session, org_and_project, parties):
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)
    await _make_commitment(
        app_session, project_id, vendor.id, internal.id, evidence_offsets_days=[10, 8, 6]
    )
    result = await compute_median_response_time_days(app_session, vendor.id)
    assert result.value == pytest.approx(2.0, abs=0.01)
    assert result.sample_size == 3


@pytest.mark.asyncio
async def test_median_response_time_unavailable_below_three_points(
    app_session, org_and_project, parties
):
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)
    await _make_commitment(app_session, project_id, vendor.id, internal.id, evidence_offsets_days=[10])
    result = await compute_median_response_time_days(app_session, vendor.id)
    assert result.value is None
    assert result.unavailable_reason


@pytest.mark.asyncio
async def test_on_time_rate_scores_delivered_and_broken_against_due_date(
    app_session, org_and_project, parties, seeded_user
):
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)

    due = NOW + timedelta(days=5)
    on_time = await _make_commitment(
        app_session, project_id, vendor.id, internal.id, state="delivered", due_at=due
    )
    late = await _make_commitment(
        app_session, project_id, vendor.id, internal.id, state="delivered", due_at=due
    )
    await _make_commitment(app_session, project_id, vendor.id, internal.id, state="broken", due_at=due)
    # No due date at all — must not count in either numerator or denominator.
    await _make_commitment(app_session, project_id, vendor.id, internal.id, state="delivered")

    # audit_log is append-only (FR-LED-12: no UPDATE after insert), so each
    # row's occurred_at must be set explicitly at construction — built
    # directly rather than via record_audit_event (which always takes
    # server_default now(), too coarse to place reliably on either side of
    # `due` within one test run).
    from app.models import AuditLog

    app_session.add(
        AuditLog(
            project_id=project_id, commitment_id=on_time.id, action="state_transition",
            actor_id=seeded_user.id, from_state="at_risk", to_state="delivered",
            occurred_at=due - timedelta(days=1),
        )
    )
    app_session.add(
        AuditLog(
            project_id=project_id, commitment_id=late.id, action="state_transition",
            actor_id=seeded_user.id, from_state="at_risk", to_state="delivered",
            occurred_at=due + timedelta(days=1),
        )
    )
    await app_session.commit()

    result = await compute_on_time_rate(app_session, vendor.id)
    # Denominator: on_time, late, broken (3) — the no-due-date row excluded.
    # Numerator: only `on_time` (delivered strictly before due_at).
    assert result.sample_size == 3
    assert result.value == pytest.approx(1 / 3, abs=0.001)


@pytest.mark.asyncio
async def test_on_time_rate_unavailable_with_no_scoreable_commitments(
    app_session, org_and_project, parties
):
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)
    await _make_commitment(app_session, project_id, vendor.id, internal.id, state="committed")
    result = await compute_on_time_rate(app_session, vendor.id)
    assert result.value is None
    assert result.unavailable_reason


@pytest.mark.asyncio
async def test_revision_churn_and_price_drift_structurally_absent_before_supersedes(
    app_session, org_and_project, parties
):
    """CLAUDE.md/Prompt 10: FR-LED-05 (supersession linking) isn't
    implemented anywhere in this build — Commitment.supersedes is never
    populated, so both metrics must report `unavailable`, never a
    fabricated 0.0 a caller could mistake for 'no churn/drift happened'."""
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)
    await _make_commitment(app_session, project_id, vendor.id, internal.id, amount=100)

    churn = await compute_revision_churn(app_session, vendor.id)
    drift = await compute_price_drift(app_session, vendor.id)
    assert churn.value is None and "FR-LED-05" in churn.unavailable_reason
    assert drift.value is None and "FR-LED-05" in drift.unavailable_reason


@pytest.mark.asyncio
async def test_revision_churn_and_price_drift_compute_once_supersedes_exists(
    app_session, org_and_project, parties
):
    """Proves the computation logic itself is correct, not just the
    unavailable-gate — a future FR-LED-05 session populating `supersedes`
    needs no change here to start getting real numbers, per this module's
    own docstring."""
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)

    original = await _make_commitment(
        app_session, project_id, vendor.id, internal.id, amount=100, state="renegotiated"
    )
    # A second, unrelated commitment for the same vendor with no revision —
    # part of the revision_churn denominator, not the numerator.
    await _make_commitment(app_session, project_id, vendor.id, internal.id, amount=50)
    revision = await _make_commitment(
        app_session, project_id, vendor.id, internal.id, amount=120,
        supersedes=[original.id],
    )

    churn = await compute_revision_churn(app_session, vendor.id)
    assert churn.value == pytest.approx(1 / 3, abs=0.001)  # 1 of 3 commitments is itself a revision
    assert churn.unavailable_reason is None

    drift = await compute_price_drift(app_session, vendor.id)
    assert drift.value == pytest.approx(20.0, abs=0.01)  # |120-100|/100 * 100
    assert drift.sample_size == 1
    assert revision.id  # keeps the fixture referenced


@pytest.mark.asyncio
async def test_deviation_frequency(app_session, org_and_project, parties, seeded_user):
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)
    from sqlalchemy import select

    from app.models import Project

    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()

    flagged = await _make_commitment(app_session, project_id, vendor.id, internal.id)
    await _make_commitment(app_session, project_id, vendor.id, internal.id)  # no deviation

    await create_manual_deviation(
        app_session,
        project=project,
        actor_id=seeded_user.id,
        class_code="delay",
        description_en="Vendor delayed shop drawing sign-off",
        commitment_id=flagged.id,
        original_text="running behind on the drawings",
    )
    await app_session.commit()

    result = await compute_deviation_frequency(app_session, vendor.id)
    assert result.value == pytest.approx(0.5, abs=0.001)
    assert result.sample_size == 2
