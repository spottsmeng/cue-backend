"""app/foresight/contradiction.py — FR-FOR-03/04 (contradiction / spec-drift
detection): rule-based direct value comparison for structured attributes,
LLM-assisted judgment (a fake client, mirroring
tests/test_document_extractor.py's FakeModelClient) for natural-language
ones. Fabricated conflicting spec claims, per Prompt 7's own testing
expectation.
"""

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.documents.models import Document, DocumentVersion, SpecClaim
from app.foresight.contradiction import scan_contradictions
from app.foresight.deviation import get_deviation_class_term
from app.foresight.models import Deviation, Risk
from app.models import Evidence, Project
from tests.conftest import set_org_context


class FakeContradictionClient:
    def __init__(self, contradicts: bool):
        self.contradicts = contradicts
        self.calls = 0

    async def complete(self, prompt: str, schema: dict) -> str:
        self.calls += 1
        return json.dumps({"contradicts": self.contradicts, "explanation": "test fixture"})


async def _make_claim(
    app_session, project_id, *, location_code, attribute, value, document_name="doc"
) -> SpecClaim:
    document = Document(project_id=project_id, name=document_name)
    app_session.add(document)
    await app_session.flush()
    version = DocumentVersion(
        document_id=document.id, version_no=1, storage_ref=f"documents/{document.id}/v1/x",
        extracted_text=f"{location_code}: {value}",
    )
    app_session.add(version)
    await app_session.flush()
    app_session.add(
        Evidence(
            document_version_id=version.id, channel="manual",
            sent_at=datetime.now(timezone.utc), language="en", original_text="uploaded",
        )
    )
    claim = SpecClaim(
        document_version_id=version.id, location_code=location_code, attribute=attribute, value=value,
    )
    app_session.add(claim)
    await app_session.flush()
    app_session.add(
        Evidence(
            spec_claim_id=claim.id, channel="manual",
            sent_at=datetime.now(timezone.utc), language="en", original_text=value,
        )
    )
    await app_session.commit()
    return claim


@pytest.mark.asyncio
async def test_rule_based_dimension_conflict_is_flagged(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()

    claim_a = await _make_claim(
        app_session, project_id, location_code="H", attribute="dimension", value="2040mm x 1040mm",
        document_name="quotation.pdf",
    )
    claim_b = await _make_claim(
        app_session, project_id, location_code="H", attribute="dimension", value="2000mm x 1040mm",
        document_name="shop-drawing.pdf",
    )

    risks = await scan_contradictions(app_session, project)
    await app_session.commit()

    assert len(risks) == 1
    risk = risks[0]
    assert risk.source == "contradiction"
    assert risk.status == "open"
    assert risk.downstream_consequence  # FR-FOR-09

    await app_session.refresh(claim_a)
    await app_session.refresh(claim_b)
    newer = claim_b if claim_b.id > claim_a.id else claim_a
    older = claim_a if newer is claim_b else claim_b
    assert newer.contradicts == older.id

    # FR-DEV-04: a contradiction auto-drafts a Deviation.
    deviations = (
        await app_session.execute(select(Deviation).where(Deviation.risk_id == risk.id))
    ).scalars().all()
    assert len(deviations) == 1
    assert deviations[0].status == "auto_drafted"
    spec_drift_term = await get_deviation_class_term(app_session, project, "spec_drift")
    assert deviations[0].class_term_id == spec_drift_term.id


@pytest.mark.asyncio
async def test_matching_dimension_values_are_not_flagged(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()

    await _make_claim(app_session, project_id, location_code="I", attribute="dimension", value="1000mm x 500mm")
    await _make_claim(app_session, project_id, location_code="I", attribute="dimension", value="1000mm x 500mm")

    risks = await scan_contradictions(app_session, project)
    assert risks == []


@pytest.mark.asyncio
async def test_llm_assisted_finish_conflict_is_flagged(app_session, org_and_project):
    """'finish' is always natural language — always the LLM-assisted path,
    never rule-based (app/foresight/contradiction.py's _rule_based_conflict
    returns None for it unconditionally)."""
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()

    await _make_claim(
        app_session, project_id, location_code="J", attribute="finish", value="Matte black powder coat",
        document_name="quotation.pdf",
    )
    await _make_claim(
        app_session, project_id, location_code="J", attribute="finish", value="Gloss white paint",
        document_name="artwork-brief.pdf",
    )

    fake = FakeContradictionClient(contradicts=True)
    risks = await scan_contradictions(app_session, project, client=fake)
    await app_session.commit()

    assert fake.calls == 1
    assert len(risks) == 1
    assert risks[0].detail["attribute"] == "finish"


@pytest.mark.asyncio
async def test_llm_assisted_finish_non_conflict_is_not_flagged(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()

    await _make_claim(
        app_session, project_id, location_code="K", attribute="finish", value="Matte black",
        document_name="quotation.pdf",
    )
    await _make_claim(
        app_session, project_id, location_code="K", attribute="finish", value="Matte black powder coat",
        document_name="artwork-brief.pdf",
    )

    fake = FakeContradictionClient(contradicts=False)
    risks = await scan_contradictions(app_session, project, client=fake)

    assert fake.calls == 1
    assert risks == []


@pytest.mark.asyncio
async def test_claims_from_the_same_document_version_are_not_compared(app_session, org_and_project):
    """Two rows on the *same* document_version_id are not a cross-document
    conflict (contradiction.py's own skip condition) — no LLM call at all."""
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()

    document = Document(project_id=project_id, name="one-doc.pdf")
    app_session.add(document)
    await app_session.flush()
    version = DocumentVersion(
        document_id=document.id, version_no=1, storage_ref="documents/x/v1/y",
        extracted_text="Location L: two finishes listed",
    )
    app_session.add(version)
    await app_session.flush()
    app_session.add(
        Evidence(
            document_version_id=version.id, channel="manual",
            sent_at=datetime.now(timezone.utc), language="en", original_text="uploaded",
        )
    )
    for value in ("Matte black", "Gloss white"):
        claim = SpecClaim(document_version_id=version.id, location_code="L", attribute="finish", value=value)
        app_session.add(claim)
        await app_session.flush()
        app_session.add(
            Evidence(
                spec_claim_id=claim.id, channel="manual",
                sent_at=datetime.now(timezone.utc), language="en", original_text=value,
            )
        )
    await app_session.commit()

    fake = FakeContradictionClient(contradicts=True)
    risks = await scan_contradictions(app_session, project, client=fake)

    assert fake.calls == 0
    assert risks == []
