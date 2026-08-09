import uuid
from datetime import datetime, timezone as dt_timezone
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_actor_id, get_project, require_project_role
from app.api.schemas import (
    DocumentLineageOut,
    DocumentOut,
    DocumentSearchResult,
    DocumentTagRequest,
    DocumentVersionOut,
    EvidenceOut,
    SpecClaimOut,
)
from app.core.db import get_session
from app.documents import service as documents_service
from app.documents.extractor import RejectedSpecClaim, extract_spec_claims
from app.documents.models import Document, DocumentVersion, SpecClaim
from app.documents.sharepoint import SharePointAdapter, get_sharepoint_adapter
from app.documents.storage import StorageBackend, get_storage_backend
from app.identity.service import WRITE_ROLES
from app.models import Evidence, Project

_require_write = require_project_role(*WRITE_ROLES)

router = APIRouter(prefix="/projects/{project_id}/documents", tags=["documents"])


async def _get_document(session: AsyncSession, project: Project, document_id: uuid.UUID) -> Document:
    document = (
        await session.execute(
            select(Document).where(Document.id == document_id, Document.project_id == project.id)
        )
    ).scalar_one_or_none()
    if document is None:
        raise HTTPException(status_code=404, detail="document not found")
    return document


async def _get_version(
    session: AsyncSession, document: Document, version_id: uuid.UUID
) -> DocumentVersion:
    version = (
        await session.execute(
            select(DocumentVersion).where(
                DocumentVersion.id == version_id, DocumentVersion.document_id == document.id
            )
        )
    ).scalar_one_or_none()
    if version is None:
        raise HTTPException(status_code=404, detail="document version not found")
    return version


def _to_version_out(
    version: DocumentVersion, document: Document, storage: StorageBackend
) -> DocumentVersionOut:
    return DocumentVersionOut(
        id=version.id,
        document_id=version.document_id,
        version_no=version.version_no,
        storage_ref=version.storage_ref,
        extracted_text=version.extracted_text,
        approved_by=version.approved_by,
        approved_at=version.approved_at,
        is_current=(document.current_version_id == version.id),
        download_url=storage.signed_url(version.storage_ref),
        created_at=version.created_at,
        updated_at=version.updated_at,
    )


async def _to_spec_claim_out(session: AsyncSession, claim: SpecClaim) -> SpecClaimOut:
    evidence_rows = (
        (await session.execute(select(Evidence).where(Evidence.spec_claim_id == claim.id)))
        .scalars()
        .all()
    )
    return SpecClaimOut(
        id=claim.id,
        document_version_id=claim.document_version_id,
        deliverable_id=claim.deliverable_id,
        location_code=claim.location_code,
        attribute=claim.attribute,
        value=claim.value,
        contradicts=claim.contradicts,
        evidence=[EvidenceOut.model_validate(e) for e in evidence_rows],
    )


def _evidence_for_upload(
    channel: str,
    sent_at: datetime | None,
    language: str,
    original_text: str | None,
    translation: str | None,
    actor_id: uuid.UUID,
) -> Evidence:
    """FR-LED-10's posture, reused for FR-DOC-01: the caller supplies real
    evidence for where this file came from; only the "manual"/no-message
    case (a caller who genuinely just picked a file and hit upload) gets a
    synthesized original_text naming the user's own action, same shape
    app/api/budget.py's _evidence_for and app/api/commitments.py's
    create_commitment already establish for evidence-less manual entry."""
    return Evidence(
        channel=channel,
        sent_at=sent_at or datetime.now(dt_timezone.utc),
        language=language,
        original_text=original_text
        or f"Document uploaded via the CUE API by user {actor_id} (FR-LED-10).",
        translation=translation,
    )


@router.get("", response_model=list[DocumentOut])
async def list_documents(
    project: Annotated[Project, Depends(get_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[Document]:
    """PRD §11.2: list."""
    documents = (
        await session.execute(
            select(Document)
            .where(Document.project_id == project.id)
            .order_by(Document.created_at.desc())
        )
    ).scalars().all()
    return list(documents)


@router.get("/search", response_model=list[DocumentSearchResult])
async def search_documents(
    project: Annotated[Project, Depends(get_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
    q: str,
) -> list[DocumentSearchResult]:
    """FR-DOC-05: lexical search now (tsvector/GIN); semantic search is
    added (the pgvector column) but not populated or queried this session."""
    rows = await documents_service.search_document_versions(session, project_id=project.id, query=q)
    return [
        DocumentSearchResult(
            document=DocumentOut.model_validate(document),
            version=_to_version_out(version, document, storage),
            rank=rank,
        )
        for version, document, rank in rows
    ]


@router.get("/{document_id}", response_model=DocumentOut)
async def read_document(
    document_id: uuid.UUID,
    project: Annotated[Project, Depends(get_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Document:
    return await _get_document(session, project, document_id)


@router.get("/{document_id}/versions", response_model=list[DocumentVersionOut])
async def list_versions(
    document_id: uuid.UUID,
    project: Annotated[Project, Depends(get_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
) -> list[DocumentVersionOut]:
    document = await _get_document(session, project, document_id)
    versions = (
        (
            await session.execute(
                select(DocumentVersion)
                .where(DocumentVersion.document_id == document.id)
                .order_by(DocumentVersion.version_no.asc())
            )
        )
        .scalars()
        .all()
    )
    return [_to_version_out(v, document, storage) for v in versions]


@router.get("/{document_id}/lineage", response_model=DocumentLineageOut)
async def read_lineage(
    document_id: uuid.UUID,
    project: Annotated[Project, Depends(get_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
) -> DocumentLineageOut:
    """FR-DOC-02/04: version lineage plus the unambiguous current pointer."""
    document = await _get_document(session, project, document_id)
    versions = (
        (
            await session.execute(
                select(DocumentVersion)
                .where(DocumentVersion.document_id == document.id)
                .order_by(DocumentVersion.version_no.asc())
            )
        )
        .scalars()
        .all()
    )
    return DocumentLineageOut(
        document_id=document.id,
        current_version_id=document.current_version_id,
        versions=[_to_version_out(v, document, storage) for v in versions],
    )


@router.post("", response_model=DocumentOut, status_code=201)
async def create_document(
    project: Annotated[Project, Depends(_require_write)],
    session: Annotated[AsyncSession, Depends(get_session)],
    actor_id: Annotated[uuid.UUID, Depends(get_actor_id)],
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
    file: Annotated[UploadFile, File()],
    name: Annotated[str, Form()],
    class_code: Annotated[str | None, Form()] = None,
    channel: Annotated[str, Form()] = "manual",
    sent_at: Annotated[datetime | None, Form()] = None,
    language: Annotated[str, Form()] = "en",
    original_text: Annotated[str | None, Form()] = None,
    translation: Annotated[str | None, Form()] = None,
    extracted_text: Annotated[str | None, Form()] = None,
) -> DocumentOut:
    """FR-DOC-01: ingestion. Real OCR/parsing is out of scope this session
    (CUE-Tech-Stack.md §2.4) — `extracted_text` is the caller-supplied plain
    text standing in for it."""
    file_bytes = await file.read()
    evidence = _evidence_for_upload(channel, sent_at, language, original_text, translation, actor_id)
    try:
        document = await documents_service.create_document(
            session,
            project=project,
            name=name,
            storage=storage,
            file_bytes=file_bytes,
            content_type=file.content_type or "application/octet-stream",
            extracted_text=extracted_text,
            class_code=class_code,
            evidence=evidence,
            actor_id=actor_id,
            filename=file.filename,
        )
    except LookupError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    await session.commit()
    return document


@router.post("/{document_id}/versions", response_model=DocumentVersionOut, status_code=201)
async def create_version(
    document_id: uuid.UUID,
    project: Annotated[Project, Depends(_require_write)],
    session: Annotated[AsyncSession, Depends(get_session)],
    actor_id: Annotated[uuid.UUID, Depends(get_actor_id)],
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
    file: Annotated[UploadFile, File()],
    channel: Annotated[str, Form()] = "manual",
    sent_at: Annotated[datetime | None, Form()] = None,
    language: Annotated[str, Form()] = "en",
    original_text: Annotated[str | None, Form()] = None,
    translation: Annotated[str | None, Form()] = None,
    extracted_text: Annotated[str | None, Form()] = None,
) -> DocumentVersionOut:
    """FR-DOC-02: a new version, correctly superseding the old one."""
    document = await _get_document(session, project, document_id)
    file_bytes = await file.read()
    evidence = _evidence_for_upload(channel, sent_at, language, original_text, translation, actor_id)
    version = await documents_service.add_version(
        session,
        project=project,
        document=document,
        storage=storage,
        file_bytes=file_bytes,
        content_type=file.content_type or "application/octet-stream",
        extracted_text=extracted_text,
        evidence=evidence,
        actor_id=actor_id,
        filename=file.filename,
    )
    await session.commit()
    return _to_version_out(version, document, storage)


@router.post("/{document_id}/versions/{version_id}/approve", response_model=DocumentVersionOut)
async def approve_version(
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    project: Annotated[Project, Depends(_require_write)],
    session: Annotated[AsyncSession, Depends(get_session)],
    actor_id: Annotated[uuid.UUID, Depends(get_actor_id)],
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
    sharepoint: Annotated[SharePointAdapter, Depends(get_sharepoint_adapter)],
) -> DocumentVersionOut:
    """FR-DOC-02/04's approval step, plus FR-DOC-07's write-back trigger."""
    document = await _get_document(session, project, document_id)
    version = await _get_version(session, document, version_id)
    version = await documents_service.approve_version(
        session, project=project, document=document, document_version=version, actor_id=actor_id,
        sharepoint=sharepoint, storage=storage,
    )
    await session.commit()
    return _to_version_out(version, document, storage)


@router.post("/{document_id}/tag", response_model=DocumentOut)
async def tag_document(
    document_id: uuid.UUID,
    body: DocumentTagRequest,
    project: Annotated[Project, Depends(_require_write)],
    session: Annotated[AsyncSession, Depends(get_session)],
    actor_id: Annotated[uuid.UUID, Depends(get_actor_id)],
) -> Document:
    """FR-DOC-03: auto-tag into deliverable_class/milestone_type/phase."""
    document = await _get_document(session, project, document_id)
    try:
        document = await documents_service.auto_tag(
            session,
            project=project,
            document=document,
            class_code=body.class_code,
            milestone_type_code=body.milestone_type_code,
            phase_code=body.phase_code,
            actor_id=actor_id,
        )
    except LookupError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    await session.commit()
    return document


@router.get("/{document_id}/versions/{version_id}/spec-claims", response_model=list[SpecClaimOut])
async def list_spec_claims(
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    project: Annotated[Project, Depends(get_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[SpecClaimOut]:
    document = await _get_document(session, project, document_id)
    version = await _get_version(session, document, version_id)
    claims = (
        (
            await session.execute(
                select(SpecClaim).where(SpecClaim.document_version_id == version.id)
            )
        )
        .scalars()
        .all()
    )
    return [await _to_spec_claim_out(session, c) for c in claims]


@router.post(
    "/{document_id}/versions/{version_id}/spec-claims/extract",
    response_model=list[SpecClaimOut],
    status_code=201,
)
async def extract_version_spec_claims(
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    project: Annotated[Project, Depends(_require_write)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[SpecClaimOut]:
    """FR-DOC-08: structured spec-claim extraction (dimensions, finishes,
    quantities, prices) — schema-constrained model output, evidence
    verified in code against DocumentVersion.extracted_text before
    persisting, exactly app/ledger/extractor.py's shape."""
    document = await _get_document(session, project, document_id)
    version = await _get_version(session, document, version_id)
    try:
        claims = await extract_spec_claims(
            session, project_id=project.id, organisation_id=project.organisation_id,
            document_version=version,
        )
    except RejectedSpecClaim as e:
        await session.rollback()
        raise HTTPException(status_code=422, detail=str(e)) from e
    await session.commit()
    return [await _to_spec_claim_out(session, c) for c in claims]
