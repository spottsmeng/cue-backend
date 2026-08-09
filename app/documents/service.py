import logging
import uuid
from datetime import datetime, timezone as dt_timezone

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.capture.media import extract_document_text, get_default_ocr_client
from app.documents.audit import record_document_audit_event
from app.documents.models import Document, DocumentVersion
from app.documents.sharepoint import SharePointAdapter
from app.documents.storage import StorageBackend
from app.models import Evidence, OntologyTerm, Project, RetentionPolicy
from app.twin.service import get_milestone_type_term

logger = logging.getLogger("cue.documents.service")

# Business logic that isn't purely HTTP request/response mapping — version
# lineage, storage orchestration, ontology-tag resolution, retention
# resolution — lives here rather than in app/api/documents.py, the same
# separation app/twin/service.py already draws for the Twin domain (contrast
# with app/api/budget.py/commitments.py, simple enough to keep inline).


class ProjectAlreadyArchived(Exception):
    """Mirrors app/twin/service.py's UnknownArchetypeCode — a plain
    exception the API layer converts to the right HTTP status, not an
    HTTPException raised from inside the service layer itself."""


async def _get_universal_ontology_term(session: AsyncSession, category: str, code: str) -> OntologyTerm:
    """Same lookup shape as app/ledger/extractor.py's
    _get_commitment_act_term (category + code + universal-core scope) —
    reused here rather than inventing a parallel helper, per Prompt 6's own
    instruction. Valid for deliverable_class and phase, both seeded
    universal-core (vertical_id AND organisation_id both NULL) for the same
    deliberate v1-simplification reason commitment_act originally was — see
    the seed migration's own comment.

    NOT valid for milestone_type: that category was re-keyed to the
    event-production vertical pack by migration eed5a4da79f6 (M1's own
    notes), so a milestone_type code needs the project-aware, three-tier
    resolution app/twin/service.py's get_milestone_type_term already
    provides — reused below for that one axis instead of this simpler
    universal-core-only query, rather than this helper silently returning
    "not found" for a term that legitimately exists at the vertical tier.
    """
    stmt = select(OntologyTerm).where(
        OntologyTerm.category == category,
        OntologyTerm.code == code,
        OntologyTerm.vertical_id.is_(None),
        OntologyTerm.organisation_id.is_(None),
    )
    term = (await session.execute(stmt)).scalar_one_or_none()
    if term is None:
        raise LookupError(f"{category} ontology term not seeded: {code!r}")
    return term


async def _next_version_no(session: AsyncSession, document_id: uuid.UUID) -> int:
    current_max = (
        await session.execute(
            select(func.max(DocumentVersion.version_no)).where(
                DocumentVersion.document_id == document_id
            )
        )
    ).scalar()
    return (current_max or 0) + 1


async def _derive_extracted_text(filename: str | None, content_type: str, file_bytes: bytes) -> str | None:
    """Prompt 11 item 6: "this is also where the Documents session's
    'OCR/parsing not yet wired' limitation... gets resolved; connect real
    capture's media pipeline output into DocumentVersion's extracted-text
    field rather than building a separate text-extraction path." Reuses
    app/capture/media.py's real extractors (PDF/Office via
    extract_document_text, images via the real OCR client) rather than this
    module inventing a second text-extraction path of its own — only called
    when the caller didn't supply extracted_text explicitly (a caller who
    did is trusted over an auto-derived guess, same as before this change).
    Returns None, not an exception, for anything neither extractor
    recognises (a plain image format not real OCR handles well, or a
    filename/content-type combination with no matching extractor) — a
    freshly-ingested document with no derivable text is still a valid,
    storable Document; this was already true before this function existed.
    """
    document_text = extract_document_text(filename, file_bytes)
    if document_text:
        return document_text
    if content_type.startswith("image/"):
        try:
            ocr_text = await get_default_ocr_client().extract_text(file_bytes)
        except Exception:
            logger.warning("OCR extraction failed for upload %r", filename, exc_info=True)
            return None
        return ocr_text or None
    return None


async def _write_version(
    session: AsyncSession,
    *,
    document: Document,
    storage: StorageBackend,
    file_bytes: bytes,
    content_type: str,
    extracted_text: str | None,
    evidence: Evidence,
    filename: str | None = None,
) -> DocumentVersion:
    """Shared by create_document (first version) and add_version (every
    version after) — one place that mints the storage key, writes the
    original bytes exactly once (never mutated afterward, per Prompt 6's own
    instruction), and satisfies DocumentVersion's NOT NULL evidence
    requirement before returning."""
    version_no = await _next_version_no(session, document.id)
    storage_ref = f"documents/{document.id}/v{version_no}/{uuid.uuid4()}"
    await storage.put(storage_ref, file_bytes, content_type)

    if extracted_text is None:
        extracted_text = await _derive_extracted_text(filename, content_type, file_bytes)

    version = DocumentVersion(
        document_id=document.id,
        version_no=version_no,
        storage_ref=storage_ref,
        extracted_text=extracted_text,
    )
    session.add(version)
    await session.flush()  # need version.id for the evidence FK below

    evidence.document_version_id = version.id
    session.add(evidence)
    await session.flush()
    return version


async def create_document(
    session: AsyncSession,
    *,
    project: Project,
    name: str,
    storage: StorageBackend,
    file_bytes: bytes,
    content_type: str,
    extracted_text: str | None,
    class_code: str | None,
    evidence: Evidence,
    actor_id: uuid.UUID | None,
    filename: str | None = None,
) -> Document:
    """FR-DOC-01/02: ingestion — a new Document plus its first
    DocumentVersion, in one call (the same "provision and populate in one
    request" shape app/api/projects.py's create_project already uses for a
    Project plus its Twin graph). `filename` (typically the uploaded file's
    own name) drives real text extraction when `extracted_text` isn't
    supplied — see _derive_extracted_text."""
    document = Document(project_id=project.id, name=name)
    if class_code is not None:
        term = await _get_universal_ontology_term(session, "deliverable_class", class_code)
        document.class_term_id = term.id
    session.add(document)
    await session.flush()  # need document.id for the version below

    version = await _write_version(
        session,
        document=document,
        storage=storage,
        file_bytes=file_bytes,
        content_type=content_type,
        extracted_text=extracted_text,
        evidence=evidence,
        filename=filename,
    )
    document.current_version_id = version.id
    await session.flush()
    # updated_at has a server-side onupdate=func.now() — after this UPDATE
    # flush, SQLAlchemy expires it; refresh explicitly before it's read
    # (e.g. by the response model), same MissingGreenlet trap
    # app/api/commitments.py's _refresh_updated_at documents.
    await session.refresh(document, attribute_names=["updated_at"])

    await record_document_audit_event(
        session, project_id=project.id, action="document_created", actor_id=actor_id,
        document_id=document.id,
    )
    await record_document_audit_event(
        session, project_id=project.id, action="version_created", actor_id=actor_id,
        document_id=document.id, document_version_id=version.id,
        detail={"version_no": version.version_no},
    )
    return document


async def add_version(
    session: AsyncSession,
    *,
    project: Project,
    document: Document,
    storage: StorageBackend,
    file_bytes: bytes,
    content_type: str,
    extracted_text: str | None,
    evidence: Evidence,
    actor_id: uuid.UUID | None,
    filename: str | None = None,
) -> DocumentVersion:
    """FR-DOC-02: a new version, correctly superseding the old one —
    `document.current_version_id` is repointed here, in the same
    transaction, never left for a caller to update separately."""
    version = await _write_version(
        session,
        document=document,
        storage=storage,
        file_bytes=file_bytes,
        content_type=content_type,
        extracted_text=extracted_text,
        evidence=evidence,
        filename=filename,
    )
    document.current_version_id = version.id
    await session.flush()
    await session.refresh(document, attribute_names=["updated_at"])

    await record_document_audit_event(
        session, project_id=project.id, action="version_created", actor_id=actor_id,
        document_id=document.id, document_version_id=version.id,
        detail={"version_no": version.version_no},
    )
    return version


async def approve_version(
    session: AsyncSession,
    *,
    project: Project,
    document: Document,
    document_version: DocumentVersion,
    actor_id: uuid.UUID,
    sharepoint: SharePointAdapter,
    storage: StorageBackend,
) -> DocumentVersion:
    """FR-DOC-02's 'on whose approval' + FR-DOC-07's write-back trigger —
    approving a version is also the moment CUE writes the canonical version
    back to SharePoint (app/documents/sharepoint.py's GraphSharePointAdapter
    for a real tenant/demo sandbox, or the no-op default).

    The approval itself is already a real, committed fact about this
    system's own state — a write-back failure (network blip, a demo
    tenant's token expiring mid-session) must not roll that back or block
    the request. The outcome is recorded on the audit trail either way
    (`detail.sharepoint_write_back`), so a failure is visible and
    diagnosable, not silently swallowed.
    """
    document_version.approved_by = actor_id
    document_version.approved_at = datetime.now(dt_timezone.utc)
    await session.flush()
    await session.refresh(document_version, attribute_names=["updated_at"])

    try:
        content = await storage.get(document_version.storage_ref)
        await sharepoint.write_back(document_version, document.name, content)
        write_back_result = "ok"
    except Exception as e:  # noqa: BLE001 — genuinely any failure mode of an external call
        logger.warning(
            "sharepoint write-back failed for document_version %s: %s", document_version.id, e
        )
        write_back_result = f"failed: {e}"

    await record_document_audit_event(
        session, project_id=project.id, action="version_approved", actor_id=actor_id,
        document_id=document_version.document_id, document_version_id=document_version.id,
        detail={"sharepoint_write_back": write_back_result},
    )
    return document_version


async def auto_tag(
    session: AsyncSession,
    *,
    project: Project,
    document: Document,
    class_code: str | None,
    milestone_type_code: str | None,
    phase_code: str | None,
    actor_id: uuid.UUID | None,
) -> Document:
    """FR-DOC-03: classify a Document into deliverable_class/milestone_type/
    phase ontology terms. Every argument optional — a caller (or, later, an
    automated classifier) may tag one axis at a time without disturbing the
    others."""
    changes: dict[str, str] = {}
    if class_code is not None:
        term = await _get_universal_ontology_term(session, "deliverable_class", class_code)
        document.class_term_id = term.id
        changes["class_term_id"] = class_code
    if milestone_type_code is not None:
        term = await get_milestone_type_term(session, project, milestone_type_code)
        document.milestone_type_term_id = term.id
        changes["milestone_type_term_id"] = milestone_type_code
    if phase_code is not None:
        term = await _get_universal_ontology_term(session, "phase", phase_code)
        document.phase_term_id = term.id
        changes["phase_term_id"] = phase_code

    await session.flush()
    await session.refresh(document, attribute_names=["updated_at"])

    await record_document_audit_event(
        session, project_id=document.project_id, action="auto_tagged", actor_id=actor_id,
        document_id=document.id, detail={"changes": changes},
    )
    return document


async def search_document_versions(
    session: AsyncSession, *, project_id: uuid.UUID, query: str
) -> list[tuple[DocumentVersion, Document, float]]:
    """FR-DOC-05: Postgres full-text search (tsvector/GIN index) over
    DocumentVersion.extracted_text, ranked by ts_rank. Semantic search (the
    pgvector `embedding` column) is added this session but left unpopulated
    — Prompt 6's own EXPLICITLY OUT OF SCOPE note; Prompt 9 (Ask &
    retrieval) is what populates and queries it."""
    tsquery = func.plainto_tsquery("english", query)
    rank = func.ts_rank(DocumentVersion.search_vector, tsquery)
    stmt = (
        select(DocumentVersion, Document, rank.label("rank"))
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(Document.project_id == project_id)
        .where(DocumentVersion.search_vector.op("@@")(tsquery))
        .order_by(rank.desc())
    )
    rows = (await session.execute(stmt)).all()
    return [(dv, d, r) for dv, d, r in rows]


async def _resolve_retention_policy(
    session: AsyncSession, project: Project
) -> RetentionPolicy | None:
    """Most-specific-match resolution over the org × vertical axes (Project
    has no `region` column of its own, so that axis is never narrowed here)
    — a RetentionPolicy row whose vertical_id matches this project's exactly
    beats one with vertical_id NULL (applies org-wide across every
    vertical), same "specific tier wins" principle
    app/twin/service.py's _resolve_archetype already applies to
    MilestoneArchetype resolution, adapted to RetentionPolicy's two-axis
    (not three-tier) shape."""
    stmt = select(RetentionPolicy).where(
        RetentionPolicy.organisation_id == project.organisation_id,
        or_(
            RetentionPolicy.vertical_id == project.vertical_id,
            RetentionPolicy.vertical_id.is_(None),
        ),
    )
    candidates = (await session.execute(stmt)).scalars().all()
    if not candidates:
        return None
    specific = [c for c in candidates if c.vertical_id == project.vertical_id]
    return specific[0] if specific else candidates[0]


async def archive_project(
    session: AsyncSession, *, project: Project, actor_id: uuid.UUID
) -> tuple[Project, RetentionPolicy | None]:
    """FR-DOC-06: archive on project close, applying the org/project's
    RetentionPolicy to the project's documents. "Applying" here means
    resolving which policy governs this project and recording that
    resolution on the audit trail — there is no deletion scheduler to hand
    it to (deliberately out of scope, same as Prompt 5's own note on
    RetentionPolicy itself: "nothing consumes this value yet"). The audit
    trail is retained intact; archiving never deletes or mutates any prior
    document_audit_log/audit_log/twin_audit_log row."""
    if project.archived_at is not None:
        raise ProjectAlreadyArchived(f"project {project.id} is already archived")

    project.archived_at = datetime.now(dt_timezone.utc)
    await session.flush()
    await session.refresh(project, attribute_names=["updated_at"])

    policy = await _resolve_retention_policy(session, project)
    await record_document_audit_event(
        session, project_id=project.id, action="project_archived", actor_id=actor_id,
        detail={
            "retention_policy_id": str(policy.id) if policy else None,
            "retention_days": policy.retention_days if policy else None,
        },
    )
    return project, policy
