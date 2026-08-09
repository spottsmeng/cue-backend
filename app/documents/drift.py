"""FR-DOC-09 (PRD §6.6): "Detect when a file circulated in chat differs
from the current approved version and flag it." Deferred to this session
from the Documents session (Prompt 6) — backend/PROGRESS.md's M3 notes, in
that session's own words: "structurally needs Prompt 11's real chat-capture
pipeline to have anything to compare against." Called from
app/capture/media_pipeline.py once a document-kind MessageMedia's original
bytes are safely stored (item 6) — never inline in a ChannelAdapter, same
"queue consumer, not adapter concern" posture every other item-6/12 step in
this package follows.

Foresight has run in this codebase (Prompt 7), so per Prompt 11's own
instruction this raises a real Risk + Deviation (the "otherwise a
Risk-shaped Notification directly" fallback is for a codebase where it
hasn't) — reusing the exact Risk -> dispatch_event -> draft_deviation chain
app/foresight/contradiction.py already establishes for its own "spec claims
disagree" finding, since a circulated file disagreeing with its own
project's approved version is the same *kind* of finding (two things that
are each claimed to be authoritative, disagreeing), just sourced from
capture instead of spec-claim extraction. `source="contradiction"` (the
existing RiskSource enum value) is used rather than adding a new enum value
for this one caller.
"""

import hashlib
import logging
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.documents.models import Document, DocumentVersion
from app.documents.storage import StorageBackend
from app.foresight.consequence import deliverable_downstream_consequence
from app.foresight.deviation import draft_deviation
from app.foresight.models import Risk
from app.foresight.notification import dispatch_event
from app.foresight.risk import create_or_supersede_risk
from app.models import Project

logger = logging.getLogger("cue.documents.drift")


async def resolve_document_for_attachment(
    session: AsyncSession, *, project_id: uuid.UUID, filename: str | None
) -> Document | None:
    """Best-effort, by Prompt 11's own explicit instruction ("an
    unresolvable attachment isn't an error, just not checked") — a
    case-insensitive exact match on Document.name against the attachment's
    own filename. Deliberately not fuzzy matching: a wrong match here would
    silently flag drift against the wrong document, which is worse than not
    checking at all.
    """
    if not filename:
        return None
    return (
        await session.execute(
            select(Document).where(
                Document.project_id == project_id, func.lower(Document.name) == filename.strip().lower()
            )
        )
    ).scalar_one_or_none()


async def check_document_drift(
    session: AsyncSession,
    *,
    project: Project,
    document: Document,
    attachment_bytes: bytes,
    storage: StorageBackend,
    original_text: str | None,
) -> Risk | None:
    """Hash comparison (Prompt 11's own "hash or content comparison"
    wording) against `document.current_version_id`'s stored bytes. No
    current version at all (a Document with no approved version yet) is not
    a drift condition — there is nothing yet to have drifted from, and a
    Document with a current version whose storage object has since gone
    missing is a storage-layer problem to log, not a drift finding to
    fabricate. Returns the Risk raised (new or already-open, per FR-FOR-10
    dedup), or None if there was nothing to check or no drift found.
    """
    if document.current_version_id is None:
        return None
    current_version = (
        await session.execute(
            select(DocumentVersion).where(DocumentVersion.id == document.current_version_id)
        )
    ).scalar_one_or_none()
    if current_version is None:
        return None

    try:
        approved_bytes = await storage.get(current_version.storage_ref)
    except FileNotFoundError:
        logger.warning(
            "drift check skipped: approved version storage object missing for document %s", document.id
        )
        return None

    if hashlib.sha256(attachment_bytes).hexdigest() == hashlib.sha256(approved_bytes).hexdigest():
        return None  # byte-identical — no drift

    consequence = await deliverable_downstream_consequence(
        session, project, None,
        because=f"A file circulated in chat for {document.name!r} differs from its approved version",
    )
    risk, created = await create_or_supersede_risk(
        session,
        project_id=project.id,
        source="contradiction",
        finding_key=f"doc_drift:{document.id}:{current_version.id}",
        severity="high",
        downstream_consequence=consequence,
        detail={
            "document_id": str(document.id),
            "document_name": document.name,
            "current_version_id": str(current_version.id),
        },
    )
    if not created:
        return risk  # FR-FOR-10: already open and unchanged since last check

    await dispatch_event(session, project=project, event_type="risk", risk=risk)
    await draft_deviation(
        session,
        project=project,
        class_code="spec_drift",
        description_en=f"Circulated file for {document.name!r} differs from the approved version",
        risk=risk,
        evidence_text=(
            original_text
            or f"A file matching document {document.name!r} was circulated in chat and differs "
            "(by hash) from the current approved version."
        ),
    )
    return risk
