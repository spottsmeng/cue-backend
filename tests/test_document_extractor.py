"""app/documents/extractor.py's own logic, using a fake LLM client — mirrors
tests/test_extractor.py's FakeModelClient pattern exactly. What's under test
is everything around the model call: code-level evidence-span verification
against DocumentVersion.extracted_text (CLAUDE.md: 'verified in code, not
trusted' — Prompt 6's own note says this bar applies to documents too, not
just chat messages), and the SpecClaim + Evidence write path.
"""

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.documents.extractor import RejectedSpecClaim, extract_spec_claims
from app.documents.models import Document, DocumentVersion, SpecClaim
from app.models import Evidence
from tests.conftest import FAKE_LLM_USAGE, set_org_context

DOCUMENT_TEXT = "Location H: 2040mm x 1040mm graphic panel, Graphic print on plywood finish, qty 1 set."


class FakeModelClient:
    """Ignores the prompt entirely — returns whatever canned response the
    test configured. Mirrors tests/test_extractor.py's FakeModelClient."""

    def __init__(self, response: dict):
        self.response = response
        self.calls: list[tuple[str, dict]] = []

    async def complete(self, prompt: str, schema: dict):
        self.calls.append((prompt, schema))
        return json.dumps(self.response), FAKE_LLM_USAGE


async def _make_document_version(app_session, project_id, text: str = DOCUMENT_TEXT) -> DocumentVersion:
    document = Document(project_id=project_id, name="Graphic list.pdf")
    app_session.add(document)
    await app_session.flush()
    version = DocumentVersion(
        document_id=document.id, version_no=1, storage_ref="documents/x/v1/y", extracted_text=text,
    )
    app_session.add(version)
    await app_session.flush()
    app_session.add(
        Evidence(
            document_version_id=version.id, channel="manual",
            sent_at=datetime.now(timezone.utc),
            language="en", original_text="Uploaded via the CUE API.",
        )
    )
    await app_session.commit()
    document.current_version_id = version.id
    await app_session.commit()
    return version


@pytest.mark.asyncio
async def test_evidence_span_not_in_document_text_is_rejected(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    version = await _make_document_version(app_session, project_id)
    fake = FakeModelClient(
        {
            "claims": [
                {
                    "attribute": "dimension",
                    "value": "2040mm x 1040mm",
                    "evidence_span": "this text does not appear in the document anywhere",
                    "confidence": 0.9,
                }
            ]
        }
    )

    with pytest.raises(RejectedSpecClaim, match="evidence_span not found verbatim"):
        await extract_spec_claims(
            app_session, project_id=project_id, organisation_id=org_id, document_version=version, client=fake,
        )

    await app_session.rollback()
    count = (await app_session.execute(select(SpecClaim))).scalars().all()
    assert count == []


@pytest.mark.asyncio
async def test_valid_extraction_writes_spec_claim_and_evidence(
    app_session, owner_session, org_and_project
):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    version = await _make_document_version(app_session, project_id)
    fake = FakeModelClient(
        {
            "claims": [
                {
                    "location_code": "H",
                    "attribute": "dimension",
                    "value": "2040mm x 1040mm",
                    "evidence_span": "2040mm x 1040mm",
                    "confidence": 0.92,
                }
            ]
        }
    )

    created = await extract_spec_claims(
        app_session, project_id=project_id, organisation_id=org_id, document_version=version, client=fake,
    )
    await app_session.commit()
    assert len(created) == 1

    # Genuinely separate session/connection, not the same app_session
    # re-queried after commit — mirrors tests/test_extractor.py's own
    # MissingGreenlet-avoidance note.
    claim = (
        await owner_session.execute(select(SpecClaim).where(SpecClaim.id == created[0].id))
    ).scalar_one()
    assert claim.location_code == "H"
    assert claim.attribute == "dimension"
    assert claim.value == "2040mm x 1040mm"
    # Blind Spots item 4: the extraction model's own confidence (the fake
    # client's canned 0.92 above) must actually reach the persisted row —
    # it used to be dropped on the floor between the schema and the model.
    assert claim.confidence == 0.92

    evidence = (
        await owner_session.execute(select(Evidence).where(Evidence.spec_claim_id == claim.id))
    ).scalar_one()
    assert evidence.original_text == DOCUMENT_TEXT
    assert (
        DOCUMENT_TEXT[evidence.span_start : evidence.span_end] == "2040mm x 1040mm"
    )


@pytest.mark.asyncio
async def test_multiple_claims_from_one_document_version(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    version = await _make_document_version(app_session, project_id)
    fake = FakeModelClient(
        {
            "claims": [
                {
                    "attribute": "dimension", "value": "2040mm x 1040mm",
                    "evidence_span": "2040mm x 1040mm", "confidence": 0.9,
                },
                {
                    "attribute": "finish", "value": "Graphic print on plywood",
                    "evidence_span": "Graphic print on plywood", "confidence": 0.85,
                },
                {
                    "attribute": "quantity", "value": "1 set",
                    "evidence_span": "qty 1 set", "confidence": 0.8,
                },
            ]
        }
    )

    created = await extract_spec_claims(
        app_session, project_id=project_id, organisation_id=org_id, document_version=version, client=fake,
    )
    await app_session.commit()

    assert {c.attribute for c in created} == {"dimension", "finish", "quantity"}
