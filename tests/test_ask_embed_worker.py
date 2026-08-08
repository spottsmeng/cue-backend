"""app/ask/embed_worker.py's embed_project — the background/on-write
embedding step (Prompt 9's own instruction: not computed synchronously on
the request path). Uses a fake embedding client, same
tests/test_extractor.py FakeModelClient pattern, so this never depends on a
live Ollama/TEI endpoint.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.ask.embed_worker import embed_project
from app.ask.models import RetrievalChunk
from app.documents.models import Document, DocumentVersion
from app.ledger.audit import record_audit_event
from app.ledger.extractor import _get_commitment_act_term
from app.models import Commitment, Evidence, Project
from tests.conftest import set_org_context


class FakeEmbeddingClient:
    def __init__(self, dimension: int = 1024):
        self.dimension = dimension
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[0.01] * self.dimension for _ in texts]


async def _make_document_version(session, project_id, text: str) -> DocumentVersion:
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


async def _make_commitment_with_evidence(session, project_id, vendor, internal) -> Commitment:
    act_term = await _get_commitment_act_term(session, "commit")
    commitment = Commitment(
        project_id=project_id, party_id=vendor.id, counterparty_id=internal.id, act_type_id=act_term.id,
        state="committed", deliverable_en="LED screen install", confidence=0.9,
        verification_state="human_verified",
    )
    session.add(commitment)
    await session.flush()
    session.add(
        Evidence(
            commitment_id=commitment.id, channel="whatsapp", sent_at=datetime.now(timezone.utc),
            language="en", original_text="LED screens confirmed for install on the 24th",
        )
    )
    await session.commit()
    return commitment


@pytest.mark.asyncio
async def test_embed_project_populates_document_version_evidence_and_audit_log(
    app_session, org_and_project, parties, seeded_user
):
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)

    await _make_document_version(app_session, project_id, "LED screen mounted on plywood backing.")
    commitment = await _make_commitment_with_evidence(app_session, project_id, vendor, internal)
    await record_audit_event(
        app_session, project_id=project_id, commitment_id=commitment.id, action="state_transition",
        actor_id=seeded_user.id, from_state="proposed", to_state="committed",
    )
    await app_session.commit()

    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    fake = FakeEmbeddingClient()
    embedded = await embed_project(app_session, project, fake)
    await app_session.commit()

    # 1 document version + 2 evidence rows (the document version's own
    # upload evidence, plus the commitment's) + 1 audit log row.
    assert embedded == 4

    version = (await app_session.execute(select(DocumentVersion))).scalar_one()
    assert version.embedding is not None
    assert len(version.embedding) == 1024

    chunks = (await app_session.execute(select(RetrievalChunk))).scalars().all()
    assert {c.source_type for c in chunks} == {"evidence", "audit_log"}
    assert all(c.embedding is not None for c in chunks)


@pytest.mark.asyncio
async def test_embed_project_is_idempotent(app_session, org_and_project, parties, seeded_user):
    org_id, project_id = org_and_project
    vendor, internal = parties
    await set_org_context(app_session, org_id)

    commitment = await _make_commitment_with_evidence(app_session, project_id, vendor, internal)
    await record_audit_event(
        app_session, project_id=project_id, commitment_id=commitment.id, action="verified",
        actor_id=seeded_user.id,
    )
    await app_session.commit()

    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    fake = FakeEmbeddingClient()
    first_pass = await embed_project(app_session, project, fake)
    await app_session.commit()
    second_pass = await embed_project(app_session, project, fake)
    await app_session.commit()

    assert first_pass == 2
    assert second_pass == 0
    chunks = (await app_session.execute(select(RetrievalChunk))).scalars().all()
    assert len(chunks) == 2
