"""app/ask/brief.py's compose_successor_brief — FR-ASK-07's "full handover
context pack in one action." Section-completeness: every one of §12.5's six
named sections (open commitments, decision history, risks, key documents,
vendor contacts, deviations and resolutions) is populated from a project
seeded with one of each.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.ask.brief import compose_successor_brief
from app.documents.models import Document, DocumentVersion
from app.foresight.deviation import draft_deviation
from app.foresight.models import Risk
from app.ledger.audit import record_audit_event
from app.ledger.extractor import _get_commitment_act_term
from app.models import Commitment, Evidence, Project
from tests.conftest import set_org_context

NOW = datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_successor_brief_populates_every_section(
    app_session, org_and_project, parties, seeded_user
):
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)

    # Open commitment + decision history.
    act_term = await _get_commitment_act_term(app_session, "commit")
    commitment = Commitment(
        project_id=project_id, party_id=vendor.id, counterparty_id=internal.id, act_type_id=act_term.id,
        state="committed", deliverable_en="LED screen install", confidence=0.9,
        verification_state="human_verified",
    )
    app_session.add(commitment)
    await app_session.flush()
    app_session.add(
        Evidence(commitment_id=commitment.id, channel="whatsapp", sent_at=NOW, language="en", original_text="ok")
    )
    await app_session.commit()
    await record_audit_event(
        app_session, project_id=project_id, commitment_id=commitment.id, action="state_transition",
        actor_id=seeded_user.id, from_state="proposed", to_state="committed",
        detail={"changes": {"deliverable_en": {"before": "LED screen", "after": "LED screen install"}}},
    )
    await app_session.commit()

    # Risk.
    app_session.add(
        Risk(
            project_id=project_id, source="silence", finding_key=f"silence:{commitment.id}", severity="high",
            status="open", commitment_id=commitment.id,
            downstream_consequence="vendor has gone quiet past the expected response window",
        )
    )
    await app_session.commit()

    # Key document.
    document = Document(project_id=project_id, name="Booth floor plan")
    app_session.add(document)
    await app_session.flush()
    version = DocumentVersion(
        document_id=document.id, version_no=1, storage_ref="documents/x/v1/y", extracted_text="plan text",
    )
    app_session.add(version)
    await app_session.flush()
    app_session.add(
        Evidence(
            document_version_id=version.id, channel="manual", sent_at=NOW, language="en",
            original_text="Uploaded via the CUE API.",
        )
    )
    await app_session.commit()
    document.current_version_id = version.id
    await app_session.commit()

    # Deviation (with a resolution).
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    deviation = await draft_deviation(
        app_session, project=project, class_code="delay", description_en="Load-in delayed by traffic",
        evidence_text="Truck stuck in traffic, ETA pushed 3 hours.",
    )
    deviation.resolution_date = NOW
    deviation.resolution_owner = seeded_user.id
    await app_session.commit()

    brief = await compose_successor_brief(app_session, project)

    assert brief.project_id == project.id
    assert [c.commitment_id for c in brief.open_commitments] == [commitment.id]
    assert any(d.action == "state_transition" for d in brief.decision_history)
    # Blind Spots item 7: the same real AuditLog.detail diff a correction
    # writes must reach the Successor Brief's own decision history, not
    # just the Decision Log report section.
    transition = next(d for d in brief.decision_history if d.action == "state_transition")
    assert transition.detail == {
        "changes": {"deliverable_en": {"before": "LED screen", "after": "LED screen install"}}
    }
    assert [r.severity for r in brief.risks] == ["high"]
    assert [d.document_id for d in brief.key_documents] == [document.id]
    assert brief.key_documents[0].approved is True
    assert [v.party_id for v in brief.vendor_contacts] == [vendor.id]
    assert brief.vendor_contacts[0].open_commitment_count == 1
    assert len(brief.deviations_and_resolutions) == 1
    assert brief.deviations_and_resolutions[0].deviation_id == deviation.id
    assert brief.deviations_and_resolutions[0].resolution_date is not None
