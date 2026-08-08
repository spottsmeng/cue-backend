"""Background/on-write embedding step (Prompt 9's own instruction: "not
computed synchronously in the request path for every read") — populates
DocumentVersion.embedding (already exists, left unpopulated by the
Documents session on purpose) plus a RetrievalChunk row per not-yet-embedded
Evidence/AuditLog row, for every project. Same periodic-sweep shape
app/foresight/worker.py's run_foresight_sweep and app/reports/schedule.py's
run_due_report_schedules already establish — registered onto that same arq
worker process (app/foresight/worker.py's WorkerSettings) rather than a
second worker for one more periodic job, per that established "ride the
existing worker, don't over-invest in scheduling machinery" convention.
"""

import logging
import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.ask.embeddings import EmbeddingClient, get_embedding_client
from app.ask.models import RetrievalChunk
from app.core.config import get_settings
from app.core.db import async_session_factory
from app.documents.models import Document, DocumentVersion
from app.models import AuditLog, Evidence, Project

logger = logging.getLogger("app.ask.embed_worker")

# Bounded per sub-step per sweep tick, same posture as everywhere else in
# this codebase that processes a potentially large backlog — a project with
# more pending rows than this catches up over several ticks rather than one
# tick blocking indefinitely.
_BATCH_SIZE = 32


async def _set_org_context(session: AsyncSession, org_id: uuid.UUID) -> None:
    """`is_local=false` — same reasoning app/foresight/worker.py's own
    _set_org_context documents: this session runs one project's full sweep
    across several sequential commits, not a single per-request
    transaction."""
    await session.execute(
        text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)}
    )


async def _discover_active_projects() -> list[tuple[uuid.UUID, uuid.UUID]]:
    """Same schema-owner discovery bypass as app/foresight/worker.py's
    _discover_active_projects and app/reports/schedule.py's
    _discover_due_schedules, and the same justification: no service-account/
    agent identity exists yet in this codebase to run a platform-level scan
    as. Every embedding read/write below still runs through the ordinary
    RLS-enforced session with that project's own org context set."""
    engine = create_async_engine(get_settings().migration_database_url)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text("SELECT organisation_id, id FROM projects WHERE archived_at IS NULL")
                )
            ).all()
            return [(row.organisation_id, row.id) for row in rows]
    finally:
        await engine.dispose()


async def _embed_pending_document_versions(
    session: AsyncSession, project: Project, client: EmbeddingClient
) -> int:
    pending = (
        await session.execute(
            select(DocumentVersion)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(
                Document.project_id == project.id,
                DocumentVersion.embedding.is_(None),
                DocumentVersion.extracted_text.is_not(None),
            )
            .limit(_BATCH_SIZE)
        )
    ).scalars().all()
    if not pending:
        return 0
    vectors = await client.embed([v.extracted_text for v in pending])
    for version, vector in zip(pending, vectors):
        version.embedding = vector
    await session.flush()
    return len(pending)


async def _evidence_for_project(session: AsyncSession, project_id: uuid.UUID) -> list[Evidence]:
    """Evidence has no project_id of its own (app/models/ledger.py's Evidence
    docstring) — scoped here via an explicit join through whichever of the
    five mutually-exclusive subject FKs the table's own CHECK constraint
    guarantees is set (commitment/budget/document_version/spec_claim/
    deviation), the same "RLS plus an independent application-level check"
    posture app/api/deps.py's require_org_administrator docstring describes
    elsewhere in this codebase.

    Evidence also has no RLS policy of its own to fall back on if this join
    were ever skipped (grep the migrations: commitments, budgets, documents,
    document_versions, spec_claims and deviations all got a tenant_isolation
    policy; evidence never did) — so this explicit project_id filter is the
    only thing standing between one project's captured messages and
    another's here, never relaxed to a plain `select(Evidence)`.
    """
    ids = (
        await session.execute(
            text(
                """
                SELECT e.id FROM evidence e
                LEFT JOIN commitments c ON c.id = e.commitment_id
                LEFT JOIN budgets b ON b.id = e.budget_id
                LEFT JOIN document_versions dv ON dv.id = e.document_version_id
                LEFT JOIN documents d ON d.id = dv.document_id
                LEFT JOIN spec_claims sc ON sc.id = e.spec_claim_id
                LEFT JOIN document_versions dv2 ON dv2.id = sc.document_version_id
                LEFT JOIN documents d2 ON d2.id = dv2.document_id
                LEFT JOIN deviations dev ON dev.id = e.deviation_id
                WHERE c.project_id = :pid OR b.project_id = :pid OR d.project_id = :pid
                   OR d2.project_id = :pid OR dev.project_id = :pid
                """
            ),
            {"pid": str(project_id)},
        )
    ).scalars().all()
    if not ids:
        return []
    return list(
        (await session.execute(select(Evidence).where(Evidence.id.in_(ids)))).scalars().all()
    )


async def _embed_pending_evidence(
    session: AsyncSession, project: Project, client: EmbeddingClient
) -> int:
    already = set(
        (
            await session.execute(
                select(RetrievalChunk.source_id).where(
                    RetrievalChunk.project_id == project.id, RetrievalChunk.source_type == "evidence"
                )
            )
        )
        .scalars()
        .all()
    )
    candidates = [e for e in await _evidence_for_project(session, project.id) if e.id not in already]
    pending = candidates[:_BATCH_SIZE]
    if not pending:
        return 0
    texts = [e.translation or e.original_text for e in pending]
    vectors = await client.embed(texts)
    for evidence, evidence_text, vector in zip(pending, texts, vectors):
        session.add(
            RetrievalChunk(
                project_id=project.id, source_type="evidence", source_id=evidence.id,
                text=evidence_text, embedding=vector,
            )
        )
    await session.flush()
    return len(pending)


def _audit_log_text(entry: AuditLog) -> str:
    parts = [entry.action]
    if entry.from_state or entry.to_state:
        parts.append(f"{entry.from_state or '?'} -> {entry.to_state or '?'}")
    if entry.detail:
        parts.append(str(entry.detail))
    return " | ".join(parts)


async def _embed_pending_audit_log(
    session: AsyncSession, project: Project, client: EmbeddingClient
) -> int:
    already = set(
        (
            await session.execute(
                select(RetrievalChunk.source_id).where(
                    RetrievalChunk.project_id == project.id, RetrievalChunk.source_type == "audit_log"
                )
            )
        )
        .scalars()
        .all()
    )
    stmt = select(AuditLog).where(AuditLog.project_id == project.id)
    if already:
        stmt = stmt.where(AuditLog.id.notin_(already))
    pending = (await session.execute(stmt.limit(_BATCH_SIZE))).scalars().all()
    if not pending:
        return 0
    texts = [_audit_log_text(a) for a in pending]
    vectors = await client.embed(texts)
    for entry, entry_text, vector in zip(pending, texts, vectors):
        session.add(
            RetrievalChunk(
                project_id=project.id, source_type="audit_log", source_id=entry.id,
                text=entry_text, embedding=vector,
            )
        )
    await session.flush()
    return len(pending)


async def embed_project(session: AsyncSession, project: Project, client: EmbeddingClient) -> int:
    """Runs every embedding sub-step for one project; returns the total rows
    newly embedded."""
    total = 0
    total += await _embed_pending_document_versions(session, project, client)
    total += await _embed_pending_evidence(session, project, client)
    total += await _embed_pending_audit_log(session, project, client)
    return total


async def run_embedding_sweep(ctx: dict | None = None) -> int:
    """The arq cron job body — also directly callable (by tests, or a
    one-off ops invocation) without a running worker/broker at all, same
    contract app/foresight/worker.py's run_foresight_sweep and
    app/reports/schedule.py's run_due_report_schedules already establish.
    Returns the number of rows newly embedded across every project."""
    client = get_embedding_client()
    targets = await _discover_active_projects()
    embedded = 0
    for organisation_id, project_id in targets:
        async with async_session_factory() as session:
            await _set_org_context(session, organisation_id)
            project = (
                await session.execute(select(Project).where(Project.id == project_id))
            ).scalar_one_or_none()
            if project is None:
                continue  # deleted between discovery and processing — skip, not an error
            try:
                embedded += await embed_project(session, project, client)
                await session.commit()
            except Exception:
                await session.rollback()
                logger.exception("embedding sweep failed for project %s", project_id)
    return embedded
