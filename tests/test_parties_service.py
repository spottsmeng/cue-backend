"""app/parties/service.py's recompute_vendor_metrics — FR-VRG-02's event-
archetype segmentation and FR-VRG-03's "write a fresh snapshot on every
recompute" (append-only, not upsert-in-place)."""

import uuid

import pytest
from sqlalchemy import select

from app.ledger.extractor import _get_commitment_act_term
from app.models import Commitment, Evidence, OntologyTerm, Project
from app.parties.models import VendorMetric
from app.parties.service import recompute_vendor_metrics
from tests.conftest import set_org_context


async def _second_project(app_session, org_id, vertical_id, *, archetype_code: str) -> Project:
    project = Project(
        id=uuid.uuid4(), organisation_id=org_id, vertical_id=vertical_id,
        name="Second Project", timezone="Asia/Singapore", archetype_code=archetype_code,
    )
    app_session.add(project)
    await app_session.flush()
    return project


async def _commitment_with_evidence(app_session, project_id, vendor_id, internal_id) -> Commitment:
    act_term = await _get_commitment_act_term(app_session, "commit")
    commitment = Commitment(
        project_id=project_id, party_id=vendor_id, counterparty_id=internal_id,
        act_type_id=act_term.id, state="committed", deliverable_en="LED screen install",
        confidence=1.0, verification_state="human_verified",
    )
    app_session.add(commitment)
    await app_session.flush()
    app_session.add(
        Evidence(
            commitment_id=commitment.id, channel="whatsapp", language="en",
            original_text="ok", sent_at=commitment.created_at,
        )
    )
    await app_session.flush()
    return commitment


@pytest.mark.asyncio
async def test_recompute_writes_overall_and_per_archetype_rows(
    app_session, org_and_project, parties, seeded_vertical_id
):
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)

    default_project = (
        await app_session.execute(select(Project).where(Project.id == project_id))
    ).scalar_one()
    default_project.archetype_code = "trade_show"
    await app_session.flush()

    second_project = await _second_project(
        app_session, org_id, seeded_vertical_id, archetype_code="gala_dinner"
    )

    await _commitment_with_evidence(app_session, project_id, vendor.id, internal.id)
    await _commitment_with_evidence(app_session, second_project.id, vendor.id, internal.id)
    await app_session.commit()

    written = await recompute_vendor_metrics(app_session, vendor.id)
    await app_session.commit()

    archetypes_seen = {row.segment_event_archetype for row in written}
    assert archetypes_seen == {None, "trade_show", "gala_dinner"}
    # One row per (metric, archetype) combination — 5 metrics x 3 segments.
    assert len(written) == 15
    assert all(row.party_id == vendor.id for row in written)
    assert all(row.organisation_id == org_id for row in written)


@pytest.mark.asyncio
async def test_recompute_denormalises_vendor_category_and_city_onto_every_row(
    app_session, org_and_project, parties
):
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)

    category_term = (
        await app_session.execute(
            select(OntologyTerm).where(OntologyTerm.category == "vendor_category").limit(1)
        )
    ).scalar_one()
    vendor.vendor_category_term_id = category_term.id
    vendor.city = "Shanghai"
    await app_session.flush()
    await _commitment_with_evidence(app_session, project_id, vendor.id, internal.id)
    await app_session.commit()

    written = await recompute_vendor_metrics(app_session, vendor.id)
    await app_session.commit()

    assert all(row.segment_vendor_category == category_term.code for row in written)
    assert all(row.segment_city == "Shanghai" for row in written)


@pytest.mark.asyncio
async def test_recompute_is_append_only_history(app_session, org_and_project, parties):
    """FR-VRG-03: a second recompute adds new rows, never overwrites the
    first — that's what makes §11.2's "history" operation meaningful."""
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)
    await _commitment_with_evidence(app_session, project_id, vendor.id, internal.id)
    await app_session.commit()

    await recompute_vendor_metrics(app_session, vendor.id)
    await app_session.commit()
    await recompute_vendor_metrics(app_session, vendor.id)
    await app_session.commit()

    rows = (
        await app_session.execute(select(VendorMetric).where(VendorMetric.party_id == vendor.id))
    ).scalars().all()
    # 5 metrics x 1 segment (no archetype set on this project) x 2 recomputes.
    assert len(rows) == 10


@pytest.mark.asyncio
async def test_recompute_is_a_noop_for_unknown_party(app_session, org_and_project):
    org_id, _project_id = org_and_project
    await set_org_context(app_session, org_id)
    written = await recompute_vendor_metrics(app_session, uuid.uuid4())
    assert written == []
