"""app/ask/retrieve.py's hybrid_retrieve — lexical matches over
DocumentVersion.search_vector and RetrievalChunk.search_vector (populated
here directly, standing in for app/ask/embed_worker.py's sweep, same
"exercise the retrieval query itself, not the whole pipeline" scoping
tests/test_document_extractor.py already uses for its own fixtures), plus
FR-ASK-03's project-scoping — a query in project A must never surface
project B's evidence, even within the same organisation (RLS alone isn't
what's being tested here; both projects are visible to the same org
context, so this specifically proves retrieve.py's own project_id filters).
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.ask.models import RetrievalChunk
from app.ask.retrieve import hybrid_retrieve
from app.documents.models import Document, DocumentVersion
from app.models import Evidence, Project
from tests.conftest import set_org_context


async def _make_document_version(session: AsyncSession, project_id, text: str) -> DocumentVersion:
    document = Document(project_id=project_id, name="Graphic list.pdf")
    session.add(document)
    await session.flush()
    version = DocumentVersion(
        document_id=document.id, version_no=1, storage_ref="documents/x/v1/y", extracted_text=text,
    )
    session.add(version)
    await session.flush()
    session.add(
        Evidence(
            document_version_id=version.id, channel="manual", sent_at=datetime.now(timezone.utc),
            language="en", original_text="Uploaded via the CUE API.",
        )
    )
    await session.commit()
    return version


@pytest.mark.asyncio
async def test_lexical_match_is_found_via_document_version(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    await _make_document_version(app_session, project_id, "LED screen mounted on plywood backing, qty 2 sets.")

    hits = await hybrid_retrieve(
        app_session, project_id=project_id, query_text="plywood", query_embedding=None
    )

    assert len(hits) == 1
    assert hits[0].source_type == "document_version"


@pytest.mark.asyncio
async def test_no_match_returns_empty(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    await _make_document_version(app_session, project_id, "LED screen mounted on plywood backing, qty 2 sets.")

    hits = await hybrid_retrieve(
        app_session, project_id=project_id, query_text="spaceship", query_embedding=None
    )

    assert hits == []


@pytest.mark.asyncio
async def test_lexical_match_is_found_via_retrieval_chunk(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    app_session.add(
        RetrievalChunk(
            project_id=project_id, source_type="audit_log", source_id=uuid.uuid4(),
            text="commitment corrected: amount changed from 5000 to 4500 SGD",
        )
    )
    await app_session.commit()

    hits = await hybrid_retrieve(
        app_session, project_id=project_id, query_text="amount changed", query_embedding=None
    )

    assert len(hits) == 1
    assert hits[0].source_type == "audit_log"


@pytest.mark.asyncio
async def test_project_isolation(app_session, org_and_project, seeded_vertical_id):
    """A RetrievalChunk in project B must never surface for a query scoped
    to project A, even though both share the same org context (RLS
    wouldn't distinguish them — this is retrieve.py's own project_id
    filter)."""
    org_id, project_a_id = org_and_project
    await set_org_context(app_session, org_id)

    project_b = Project(
        organisation_id=org_id, vertical_id=seeded_vertical_id, name="Project B", timezone="Asia/Singapore",
    )
    app_session.add(project_b)
    await app_session.flush()
    app_session.add(
        RetrievalChunk(
            project_id=project_b.id, source_type="audit_log", source_id=uuid.uuid4(),
            text="a very distinctive marker phrase only in project B",
        )
    )
    await app_session.commit()

    hits_a = await hybrid_retrieve(
        app_session, project_id=project_a_id, query_text="distinctive marker phrase", query_embedding=None
    )
    hits_b = await hybrid_retrieve(
        app_session, project_id=project_b.id, query_text="distinctive marker phrase", query_embedding=None
    )

    assert hits_a == []
    assert len(hits_b) == 1
