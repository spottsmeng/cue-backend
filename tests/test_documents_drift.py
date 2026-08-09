"""FR-DOC-09: app/documents/drift.py — a file circulated in chat compared
against its resolved Document's current approved version. Real MinIO
storage (both the approved version and the "circulated" bytes), real hash
comparison, real Risk/Deviation rows on a mismatch.
"""

from datetime import datetime, timezone as dt_timezone

import pytest
from sqlalchemy import select

from app.documents.drift import check_document_drift, resolve_document_for_attachment
from app.documents.models import Document
from app.documents.storage import get_storage_backend
from app.documents.service import create_document
from app.foresight.models import Deviation, Risk
from app.models import Evidence, Project
from tests.conftest import set_org_context


async def _seed_document(app_session, project, *, name: str, content: bytes) -> Document:
    evidence = Evidence(
        channel="manual", sent_at=datetime.now(dt_timezone.utc), language="en",
        original_text="manual upload for drift test",
    )
    document = await create_document(
        app_session,
        project=project,
        name=name,
        storage=get_storage_backend(),
        file_bytes=content,
        content_type="application/pdf",
        extracted_text="stand-in text",
        class_code=None,
        evidence=evidence,
        actor_id=None,
        filename=name,
    )
    await app_session.commit()
    return document


@pytest.mark.asyncio
async def test_resolve_document_for_attachment_matches_case_insensitively(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    await _seed_document(app_session, project, name="Graphic List.pdf", content=b"original content v1")

    found = await resolve_document_for_attachment(app_session, project_id=project_id, filename="graphic list.pdf")
    assert found is not None
    assert found.name == "Graphic List.pdf"


@pytest.mark.asyncio
async def test_resolve_document_for_attachment_returns_none_when_no_match(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    assert await resolve_document_for_attachment(app_session, project_id=project_id, filename="nope.pdf") is None
    assert await resolve_document_for_attachment(app_session, project_id=project_id, filename=None) is None


@pytest.mark.asyncio
async def test_no_drift_when_bytes_identical(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    content = b"%PDF-1.4 identical content"
    document = await _seed_document(app_session, project, name="Quote.pdf", content=content)

    risk = await check_document_drift(
        app_session, project=project, document=document, attachment_bytes=content,
        storage=get_storage_backend(), original_text="here's the quote",
    )
    assert risk is None

    risks = (await app_session.execute(select(Risk).where(Risk.project_id == project_id))).scalars().all()
    assert risks == []


@pytest.mark.asyncio
async def test_drift_detected_creates_risk_and_deviation(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    document = await _seed_document(app_session, project, name="Floor Plan.pdf", content=b"approved version bytes")

    risk = await check_document_drift(
        app_session, project=project, document=document, attachment_bytes=b"a DIFFERENT circulated version",
        storage=get_storage_backend(), original_text="here's the updated floor plan",
    )
    await app_session.commit()

    assert risk is not None
    assert risk.source == "contradiction"
    assert risk.severity == "high"
    assert risk.status == "open"
    assert "Floor Plan.pdf" in risk.downstream_consequence or document.name in str(risk.detail)

    deviations = (
        await app_session.execute(select(Deviation).where(Deviation.project_id == project_id))
    ).scalars().all()
    assert len(deviations) == 1
    assert deviations[0].risk_id == risk.id
    assert deviations[0].status == "auto_drafted"


@pytest.mark.asyncio
async def test_repeated_drift_check_deduplicates_via_fr_for_10(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    document = await _seed_document(app_session, project, name="Signage.pdf", content=b"approved bytes")

    first = await check_document_drift(
        app_session, project=project, document=document, attachment_bytes=b"drifted copy",
        storage=get_storage_backend(), original_text="v1 differs",
    )
    await app_session.commit()
    second = await check_document_drift(
        app_session, project=project, document=document, attachment_bytes=b"drifted copy",
        storage=get_storage_backend(), original_text="v1 differs again",
    )
    await app_session.commit()

    assert first.id == second.id  # same open Risk, not a second one
    deviations = (
        await app_session.execute(select(Deviation).where(Deviation.project_id == project_id))
    ).scalars().all()
    assert len(deviations) == 1  # no second deviation drafted for the unchanged finding


@pytest.mark.asyncio
async def test_no_drift_check_when_document_has_no_current_version(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    bare_document = Document(project_id=project_id, name="No Version Yet.pdf")
    app_session.add(bare_document)
    await app_session.commit()

    risk = await check_document_drift(
        app_session, project=project, document=bare_document, attachment_bytes=b"anything",
        storage=get_storage_backend(), original_text=None,
    )
    assert risk is None
