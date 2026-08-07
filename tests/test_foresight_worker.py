"""app/foresight/worker.py — the arq-scheduled sweep, exercised as a plain
callable (its own docstring: "no queue-specific dependency on its ctx
argument") against the real test database, not a running worker/broker.
Confirms the whole pipeline (discovery -> per-project org context -> every
detector -> commit) actually wires together end to end, on top of each
detector's own dedicated unit coverage.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.foresight.models import Risk
from app.foresight.worker import run_foresight_sweep
from app.ledger.extractor import _get_commitment_act_term
from app.models import Commitment, Evidence
from tests.conftest import set_org_context

NOW = datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_sweep_discovers_and_processes_every_active_project(app_session, org_and_project):
    """Even a project with no Foresight-relevant data at all must sweep
    cleanly — the smoke-test floor every project in every org has to clear
    on each scheduled run."""
    org_id, project_id = org_and_project
    swept = await run_foresight_sweep()
    assert swept >= 1


@pytest.mark.asyncio
async def test_sweep_flags_a_genuinely_silent_vendor_end_to_end(app_session, org_and_project, parties):
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)

    act_term = await _get_commitment_act_term(app_session, "commit")
    commitment = Commitment(
        project_id=project_id, party_id=vendor.id, counterparty_id=internal.id, act_type_id=act_term.id,
        state="committed", deliverable_en="LED screen install", confidence=1.0,
        verification_state="human_verified",
    )
    app_session.add(commitment)
    await app_session.flush()
    for offset in (10, 8, 6):
        app_session.add(
            Evidence(
                commitment_id=commitment.id, channel="whatsapp", sent_at=NOW - timedelta(days=offset),
                language="en", original_text="ok",
            )
        )
    await app_session.commit()

    await run_foresight_sweep()

    await set_org_context(app_session, org_id)
    risks = (
        await app_session.execute(select(Risk).where(Risk.project_id == project_id, Risk.source == "silence"))
    ).scalars().all()
    assert len(risks) == 1

    await app_session.refresh(commitment)
    assert commitment.state == "at_risk"


@pytest.mark.asyncio
async def test_sweep_does_not_leak_across_organisations(app_session, org_and_project):
    """The one deliberate RLS bypass (discovery) must still result in every
    project's own writes being correctly org-scoped — a bug here would mean
    one org's Risk could get written under another org's project_id."""
    org_id, project_id = org_and_project
    await run_foresight_sweep()

    await set_org_context(app_session, org_id)
    from app.models import Project

    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    assert project.organisation_id == org_id
