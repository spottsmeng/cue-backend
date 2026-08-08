"""PRD §5.1's RAG box: "Retrieval index, hybrid lexical + semantic," sitting
between LED/DOC and the API. One retrieval function — `hybrid_retrieve` —
not two systems (pgvector, Postgres full-text) a caller has to merge
themselves, combining both signals over both places retrievable text
actually lives: DocumentVersion's own embedding/search_vector columns
(app/documents/models.py, populated by app/ask/embed_worker.py) and
RetrievalChunk (app/ask/models.py, Evidence/AuditLog text).

Fusion is Reciprocal Rank Fusion (RRF) — sum of 1/(k + rank) across every
ranked list a candidate id appears in — rather than trying to normalise
ts_rank (unbounded, corpus-dependent) against cosine distance (bounded
[0, 2]) onto one comparable scale. RRF only needs each signal's *ordering*,
not its raw magnitude, which is what both signals can actually be trusted to
give.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ask.models import RetrievalChunk
from app.documents.models import Document, DocumentVersion

_RRF_K = 60
# How many candidates each individual ranked list (lexical/semantic, per
# table) contributes before fusion — wider than the final `limit` so a
# candidate ranked highly on one signal but absent from the other still gets
# a fair shot at the merged top N.
_CANDIDATE_POOL_MULTIPLIER = 4


@dataclass
class RetrievalHit:
    source_type: str  # "document_version" | "evidence" | "audit_log"
    source_id: uuid.UUID
    project_id: uuid.UUID
    text: str
    score: float
    document_id: uuid.UUID | None = None


def _rrf_merge(*ranked_id_lists: list[uuid.UUID], k: int = _RRF_K) -> dict[uuid.UUID, float]:
    scores: dict[uuid.UUID, float] = {}
    for ranked in ranked_id_lists:
        for rank, id_ in enumerate(ranked):
            scores[id_] = scores.get(id_, 0.0) + 1.0 / (k + rank + 1)
    return scores


async def hybrid_retrieve(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    query_text: str,
    query_embedding: list[float] | None,
    limit: int = 8,
) -> list[RetrievalHit]:
    """FR-ASK-01/03: every candidate is scoped to `project_id` at the query
    level (never a broader select filtered client-side) — the same RLS +
    membership-scoped session every other endpoint uses is what's passed in
    here, so this is belt-and-suspenders with RLS on the tables that have a
    policy, and the *only* scoping on retrieval_chunks/document_versions'
    join path where a table's isolation is join-based rather than a direct
    RLS-checked column.

    `query_embedding` is `None` when the embedding client couldn't be
    reached (see app/ask/answer.py's degrade-to-lexical-only handling) —
    every semantic leg below is simply skipped in that case, not an error.
    """
    tsquery = func.plainto_tsquery("english", query_text)
    pool = limit * _CANDIDATE_POOL_MULTIPLIER

    dv_lexical_stmt = (
        select(DocumentVersion.id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(Document.project_id == project_id, DocumentVersion.search_vector.op("@@")(tsquery))
        .order_by(func.ts_rank(DocumentVersion.search_vector, tsquery).desc())
        .limit(pool)
    )
    dv_lexical_ids = list((await session.execute(dv_lexical_stmt)).scalars().all())

    dv_semantic_ids: list[uuid.UUID] = []
    if query_embedding is not None:
        dv_semantic_stmt = (
            select(DocumentVersion.id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(Document.project_id == project_id, DocumentVersion.embedding.is_not(None))
            .order_by(DocumentVersion.embedding.cosine_distance(query_embedding))
            .limit(pool)
        )
        dv_semantic_ids = list((await session.execute(dv_semantic_stmt)).scalars().all())

    rc_lexical_stmt = (
        select(RetrievalChunk.id)
        .where(RetrievalChunk.project_id == project_id, RetrievalChunk.search_vector.op("@@")(tsquery))
        .order_by(func.ts_rank(RetrievalChunk.search_vector, tsquery).desc())
        .limit(pool)
    )
    rc_lexical_ids = list((await session.execute(rc_lexical_stmt)).scalars().all())

    rc_semantic_ids: list[uuid.UUID] = []
    if query_embedding is not None:
        rc_semantic_stmt = (
            select(RetrievalChunk.id)
            .where(RetrievalChunk.project_id == project_id, RetrievalChunk.embedding.is_not(None))
            .order_by(RetrievalChunk.embedding.cosine_distance(query_embedding))
            .limit(pool)
        )
        rc_semantic_ids = list((await session.execute(rc_semantic_stmt)).scalars().all())

    scores = _rrf_merge(dv_lexical_ids, dv_semantic_ids, rc_lexical_ids, rc_semantic_ids)
    if not scores:
        return []

    dv_ids = {*dv_lexical_ids, *dv_semantic_ids}
    rc_ids = {*rc_lexical_ids, *rc_semantic_ids}

    dv_rows: dict[uuid.UUID, DocumentVersion] = {}
    if dv_ids:
        rows = (
            await session.execute(select(DocumentVersion).where(DocumentVersion.id.in_(dv_ids)))
        ).scalars().all()
        dv_rows = {r.id: r for r in rows}

    rc_rows: dict[uuid.UUID, RetrievalChunk] = {}
    if rc_ids:
        rows = (
            await session.execute(select(RetrievalChunk).where(RetrievalChunk.id.in_(rc_ids)))
        ).scalars().all()
        rc_rows = {r.id: r for r in rows}

    ranked_ids = sorted(scores, key=lambda i: scores[i], reverse=True)[:limit]

    hits: list[RetrievalHit] = []
    for id_ in ranked_ids:
        if id_ in dv_rows:
            dv = dv_rows[id_]
            hits.append(
                RetrievalHit(
                    source_type="document_version", source_id=dv.id, project_id=project_id,
                    text=dv.extracted_text or "", score=scores[id_], document_id=dv.document_id,
                )
            )
        elif id_ in rc_rows:
            rc = rc_rows[id_]
            hits.append(
                RetrievalHit(
                    source_type=rc.source_type, source_id=rc.source_id, project_id=project_id,
                    text=rc.text, score=scores[id_],
                )
            )
    return hits
