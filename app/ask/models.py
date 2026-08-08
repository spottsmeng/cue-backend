import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import Computed, Enum, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import ARRAY, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base, Timestamped, TZDateTime, UUIDPk

# Ask-domain models (PRD §6.11, FR-ASK — Prompt 9) live here, not in
# app/models/ledger.py — same per-domain-module-owns-its-models precedent
# app/twin/models.py, app/documents/models.py and app/foresight/models.py
# already establish for their own domains.

RetrievalSourceType = Enum("evidence", "audit_log", name="retrieval_source_type")


class RetrievalChunk(Base, UUIDPk):
    """The hybrid retrieval index for the two source kinds that don't
    already carry their own pgvector/tsvector columns. DocumentVersion
    (app/documents/models.py) already has both `embedding` and
    `search_vector` — this table is deliberately NOT a second copy of that
    text (Prompt 9's own instruction: "don't add a second vector column
    elsewhere; use the one that's already there"). What it *is* for:
    Evidence (the captured message text — FR-ASK-01's "communications", and
    every Commitment's grounding since CLAUDE.md requires one evidence row
    per commitment) and AuditLog (decision history) — neither has anywhere
    else to hold an embedding/tsvector without widening a table several
    other domains already depend on.

    `source_type` + `source_id` point back at the real row this chunk was
    derived from — every citation app/ask/answer.py or app/ask/brief.py ever
    returns resolves to one of these (or a DocumentVersion row, queried
    directly), never a synthetic id. `project_id` is a plain, direct
    RLS-scope column — unlike Evidence itself, which has none (see
    app/ask/embed_worker.py's own note on why embedding Evidence text means
    an explicit project-scoped join, not a query against Evidence's own
    [nonexistent] RLS policy).

    Populated by app/ask/embed_worker.py's periodic sweep, not synchronously
    on the request path that creates the underlying Evidence/AuditLog row
    (Prompt 9's own instruction) — the unique index on (source_type,
    source_id) makes a re-run of that sweep an upsert-by-skip
    (already-chunked rows are simply not re-selected), not a duplicate.
    """

    __tablename__ = "retrieval_chunks"
    __table_args__ = (
        Index("ix_retrieval_chunks_source", "source_type", "source_id", unique=True),
        Index("ix_retrieval_chunks_search_vector", "search_vector", postgresql_using="gin"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), index=True, comment="RLS scope"
    )
    source_type: Mapped[str] = mapped_column(RetrievalSourceType)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    text: Mapped[str] = mapped_column(comment="the text that was embedded/indexed")
    # GENERATED ALWAYS AS ... STORED, same shape DocumentVersion.search_vector
    # already establishes — recomputed by Postgres itself, never written to
    # by application code.
    search_vector: Mapped[str] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', coalesce(text, ''))", persisted=True)
    )
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1024), default=None)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())


class AskConversation(Base, UUIDPk, Timestamped):
    """FR-ASK-08: the session-boundary unit for follow-up/clarification.

    Session-boundary rule, decided and documented here since it governs both
    this model and app/ask/answer.py's follow-up context lookup: an EXPLICIT
    session id, not time-based expiry. A caller either omits
    `conversation_id` (a fresh conversation is created and its id returned
    for the client to reuse on the next call) or passes one back — there is
    no server-side TTL that silently ends a conversation after N idle
    minutes. Chosen over time-based expiry because it's deterministic (a
    client's own "New chat" action is what actually ends a session in every
    real Ask UI §12.5 describes, not a clock) and trivially testable —
    "is this still the same session" never depends on wall-clock timing in
    either the test suite or production, the same "don't fabricate what you
    don't have a real signal for" posture this codebase applies elsewhere
    (e.g. Risk.base_rate staying null rather than guessed).
    """

    __tablename__ = "ask_conversations"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), index=True, comment="RLS scope"
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), index=True)


class AskTurn(Base, UUIDPk):
    """One question+answer pair — FR-ASK-08's own persisted history, and
    what app/ask/answer.py's follow-up handling reads back (the most recent
    turns on the same conversation_id) so a later query can resolve "it".
    UUIDPk only (no Timestamped), same shape AuditLog/DocumentAuditLog/
    ForesightAuditLog already use for an append-style log with its own
    `asked_at` rather than created_at/updated_at.
    """

    __tablename__ = "ask_turns"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ask_conversations.id"), index=True
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), index=True, comment="RLS scope"
    )
    question: Mapped[str] = mapped_column()
    answer_available: Mapped[bool] = mapped_column(
        comment="FR-ASK-02: false means the typed 'no source' variant was returned, never prose"
    )
    answer_text: Mapped[str | None] = mapped_column(default=None)
    citation_source_types: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    citation_source_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), default=list)
    asked_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    asked_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
