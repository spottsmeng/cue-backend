"""app/foresight/silence.py — FR-FOR-01/02 (Silence Radar) and the
committed -> at_risk half of FR-LCY-02, against synthetic Evidence
timestamps (CLAUDE.md/Prompt 7's own testing expectation: real fixtures,
not mocks).
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.foresight.models import Risk
from app.foresight.silence import compute_vendor_baseline, scan_silence
from app.ledger.extractor import _get_commitment_act_term
from app.models import AuditLog, Commitment, Evidence, Project
from tests.conftest import set_org_context

NOW = datetime.now(timezone.utc)


async def _make_committed_commitment(
    app_session, project_id, vendor_id, internal_id, *, evidence_offsets_days: list[float], state="committed"
) -> Commitment:
    act_term = await _get_commitment_act_term(app_session, "commit")
    commitment = Commitment(
        project_id=project_id,
        party_id=vendor_id,
        counterparty_id=internal_id,
        act_type_id=act_term.id,
        state=state,
        deliverable_en="LED screen install",
        confidence=1.0,
        verification_state="human_verified",
    )
    app_session.add(commitment)
    await app_session.flush()
    for offset in evidence_offsets_days:
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
async def test_no_baseline_when_fewer_than_three_evidence_rows(app_session, org_and_project, parties):
    org_id, project_id = org_and_project
    vendor, _internal = parties
    await set_org_context(app_session, org_id)
    baseline = await compute_vendor_baseline(app_session, project_id, vendor.id)
    assert baseline is None


@pytest.mark.asyncio
async def test_baseline_is_median_gap_between_evidence(app_session, org_and_project, parties):
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)
    await _make_committed_commitment(
        app_session, project_id, vendor.id, internal.id, evidence_offsets_days=[10, 8, 6]
    )
    baseline = await compute_vendor_baseline(app_session, project_id, vendor.id)
    assert baseline == pytest.approx(2.0, abs=0.01)


@pytest.mark.asyncio
async def test_scan_silence_flags_gap_past_baseline_and_computes_consequence(
    app_session, org_and_project, parties
):
    """FR-FOR-01/09: a vendor whose baseline is ~2 days between contacts,
    silent for 6, gets flagged — with a real, non-empty downstream
    consequence (never a bare alert)."""
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()

    commitment = await _make_committed_commitment(
        app_session, project_id, vendor.id, internal.id, evidence_offsets_days=[10, 8, 6]
    )

    risks = await scan_silence(app_session, project)
    await app_session.commit()

    assert len(risks) == 1
    risk = risks[0]
    assert risk.source == "silence"
    assert risk.status == "open"
    assert risk.commitment_id == commitment.id
    assert risk.downstream_consequence  # FR-FOR-09: never bare/empty
    assert risk.detail["gap_days"] == pytest.approx(6.0, abs=0.05)
    assert risk.base_rate is None  # FR-FOR-02: fewer than 3 prior outcomes — honestly no rate yet


@pytest.mark.asyncio
async def test_scan_silence_triggers_committed_to_at_risk_lifecycle_transition(
    app_session, org_and_project, parties
):
    """FR-LCY-02: silence past baseline is a named automatic trigger —
    exercises app/ledger/lifecycle.py's apply_automatic_transition end to
    end, including the actor_id=None audit convention."""
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    commitment = await _make_committed_commitment(
        app_session, project_id, vendor.id, internal.id, evidence_offsets_days=[10, 8, 6]
    )

    await scan_silence(app_session, project)
    await app_session.commit()

    await app_session.refresh(commitment)
    assert commitment.state == "at_risk"

    audit_rows = (
        await app_session.execute(
            select(AuditLog).where(
                AuditLog.commitment_id == commitment.id, AuditLog.action == "state_transition"
            )
        )
    ).scalars().all()
    assert len(audit_rows) == 1
    audit = audit_rows[0]
    assert audit.actor_id is None  # system-triggered, no human behind it
    assert audit.from_state == "committed"
    assert audit.to_state == "at_risk"
    assert audit.detail["trigger"] == "silence"


@pytest.mark.asyncio
async def test_scan_silence_does_not_duplicate_an_unchanged_open_risk(app_session, org_and_project, parties):
    """FR-FOR-10 exercised end to end: re-scanning after the commitment has
    already transitioned to at_risk finds nothing new to flag (it's no
    longer `committed`, so it's outside scan_silence's own scope) — the
    net effect either way is exactly one Risk row for this finding, never
    two. The dedup mechanism itself (same finding_key while still `open`)
    is unit-tested directly in test_foresight_risk_dedup.py, since a
    commitment that stays `committed` across two scans with the exact same
    gap is the only way to exercise the *supersede-vs-noop* branch, and
    scan_silence's own state-filtering makes that unreachable through the
    detector's own scan function."""
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    await _make_committed_commitment(
        app_session, project_id, vendor.id, internal.id, evidence_offsets_days=[10, 8, 6]
    )

    first = await scan_silence(app_session, project)
    await app_session.commit()
    second = await scan_silence(app_session, project)
    await app_session.commit()

    assert len(first) == 1
    assert second == []  # commitment is no longer `committed` after the first scan's transition

    all_silence_risks = (
        await app_session.execute(select(Risk).where(Risk.project_id == project_id, Risk.source == "silence"))
    ).scalars().all()
    assert len(all_silence_risks) == 1


@pytest.mark.asyncio
async def test_scan_silence_ignores_commitments_with_no_computable_baseline(
    app_session, org_and_project, parties
):
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    # Only one evidence row — not enough for any baseline (FR-FOR-02: degrade,
    # don't fabricate).
    await _make_committed_commitment(
        app_session, project_id, vendor.id, internal.id, evidence_offsets_days=[10]
    )

    risks = await scan_silence(app_session, project)
    assert risks == []
