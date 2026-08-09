import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import Computed, Enum, ForeignKey, Index, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base, Timestamped, TZDateTime, UUIDPk

# Document-domain models (PRD §6.6, FR-DOC — Prompt 6/"Phase 8: Documents")
# live here, not in app/models/ledger.py — same reasoning app/twin/models.py's
# and app/models/governance.py's own top-of-file notes give for their
# domains: ledger.py's own scope comment already carves Document out
# explicitly ("not modelled in this pass — belong to later build phases
# (... Documents ...)"), and this is that later phase.
#
# The shared `evidence` table (app/models/ledger.py) gained two more nullable
# subject columns for this session — document_version_id and spec_claim_id —
# rather than either domain inventing its own evidence table. See that
# model's docstring and the migration that added them for why: the same
# "verified in code, not trusted" discipline CLAUDE.md states for extraction
# evidence spans applies here (a SpecClaim's evidence_span is checked against
# the source DocumentVersion.extracted_text exactly the way a Commitment's is
# checked against the source message, in app/documents/extractor.py).


class Document(Base, UUIDPk, Timestamped):
    """PRD §4.1: PROJECT ||--o{ DOCUMENT; DOCUMENT ||--o{ DOCUMENT_VERSION.

    `current_version_id` is the "unambiguous current approved version"
    pointer FR-DOC-04 asks for — a plain nullable FK to document_versions,
    added via a separate ALTER TABLE in the migration (the two tables have a
    genuine circular reference: DocumentVersion also points back at its
    parent Document). NULL until the first version is uploaded; from then on
    it's repointed by app/documents/service.py's create_version, never
    inferred by "highest version_no" at read time — an explicit pointer is
    what "unambiguous" means here, the same reasoning MilestoneArchetype's
    is_default flag is an explicit column rather than a query-time guess.

    `class_term_id` / `milestone_type_term_id` / `phase_term_id` are
    FR-DOC-03's auto-tag targets (app/documents/service.py's auto_tag) — all
    three are ontology_terms lookups (categories 'deliverable_class',
    'milestone_type', 'phase' respectively), reusing the existing
    vertical/organisation-layered vocabulary rather than inventing a parallel
    classification scheme. All nullable: a freshly-ingested document is
    untagged until auto_tag runs (or a caller supplies class_code at
    upload), same "confidence starts at zero, verification happens after"
    posture Commitment's own extraction fields already have.
    """

    __tablename__ = "documents"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), index=True, comment="RLS scope"
    )
    name: Mapped[str] = mapped_column(comment="display title, e.g. original filename")
    class_term_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ontology_terms.id"),
        default=None,
        comment="ontology_terms row, category='deliverable_class'",
    )
    milestone_type_term_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ontology_terms.id"),
        default=None,
        comment="ontology_terms row, category='milestone_type'",
    )
    phase_term_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ontology_terms.id"),
        default=None,
        comment="ontology_terms row, category='phase'",
    )
    # FK added by a separate ALTER TABLE in the migration, after both tables
    # exist — see this class's own docstring.
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), default=None, comment="FK to document_versions.id, added post-creation"
    )


class DocumentVersion(Base, UUIDPk, Timestamped):
    """PRD §4.1: DOCUMENT ||--o{ DOCUMENT_VERSION; DOCUMENT_VERSION ||--o{ SPEC_CLAIM.

    Version lineage (FR-DOC-02) is `document_id` + `version_no`, ordered —
    "which version superseded which" is simply every prior version_no on the
    same document_id; there is no separate `supersedes` column the way
    Commitment has one, because a document version's history is strictly
    linear (unlike a commitment's, which can branch via renegotiation).
    "On whose approval" is `approved_by`/`approved_at`; "evidenced by which
    message" is this row's own Evidence (below) — FR-DOC-02's three
    lineage facts are three columns/relations on this one row, not a
    separate history table.

    Evidence is **NOT NULL** here, enforced by the same deferred-constraint-
    trigger mechanism as Commitment/Budget (app/models/ledger.py's Evidence
    docstring, and the migration's check_document_version_has_evidence
    function) — per Prompt 6's explicit instruction, even though PRD §4.1's
    own ER diagram marks DOCUMENT_VERSION's evidence link optional
    (`}o--o|`). The evidence is whatever grounds this version existing: the
    message a vendor sent the file in, or (FR-LED-10's "manually-entered-
    but-fully-real" posture, reused here) the uploading user's own action
    when there's no message to point at.

    OCR/document-parsing (FR-NRM-05) is now real, not a caller-supplied
    stand-in — app/documents/service.py's `_derive_extracted_text` calls
    into app/capture/media.py (Prompt 11 item 6) for PDF/Office text and
    image OCR whenever a caller doesn't supply `extracted_text` explicitly.
    CUE-Tech-Stack.md §2.4 names PaddleOCR/Docling as the real *production*
    choice for CJK OCR and table-fidelity document parsing specifically —
    what's actually wired is the real, lighter FOSS-substitute posture
    app/capture/media.py's own module docstring explains (Tesseract,
    pdftotext, python-docx/pptx/openpyxl), not those two libraries
    specifically. An explicitly caller-supplied `extracted_text` still wins
    over auto-derivation (unchanged from before this session). `search_vector`
    and `embedding` are both derived from this same field, so both are only
    as complete as whichever extractor (or caller override) produced it.
    """

    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version_no", name="document_version_number_unique"),
        Index("ix_document_versions_search_vector", "search_vector", postgresql_using="gin"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id"), index=True
    )
    version_no: Mapped[int] = mapped_column()
    storage_ref: Mapped[str] = mapped_column(
        comment="StorageBackend object key (app/documents/storage.py) — original bytes, never mutated"
    )
    extracted_text: Mapped[str | None] = mapped_column(
        default=None,
        comment="caller-supplied stand-in for real OCR/parsing output — see class docstring",
    )
    # GENERATED ALWAYS AS ... STORED: recomputed by Postgres itself whenever
    # extracted_text changes, never written to by application code (the
    # `Computed()` value is server-side, same "app never sets it" shape
    # created_at/updated_at's server_default already has elsewhere in this
    # codebase).
    search_vector: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', coalesce(extracted_text, ''))", persisted=True),
    )
    # FR-DOC-05's semantic-search column — added now, deliberately left
    # unpopulated this session (Prompt 6's own EXPLICITLY OUT OF SCOPE note):
    # the embedding client is Prompt 9's (Ask & retrieval) job, and building
    # the column here means that milestone needs no migration of its own.
    # Dimension 1024 matches BGE-M3 (CUE-Tech-Stack.md §2.4's named
    # production embedding model).
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1024), default=None)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), default=None
    )
    approved_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)


SpecClaimAttribute = Enum("dimension", "finish", "quantity", "price", name="spec_claim_attribute")


class SpecClaim(Base, UUIDPk):
    """CUE-PRD.md §4.3's Spec Claim schema, verbatim — sourced from the
    Challenge Brief's Annex A Graphic List (`Location · Description ·
    Dimension (mm) · Finishing · Qty · Status`). Do not add or rename
    fields here; §4.3's own note says this schema already went through one
    iteration against that real worked example.

    Evidence is **NOT NULL** (§4.3: "same rule as Commitment/Budget; sourced
    from the document version itself") — each claim's evidence_span is
    verified in code against its DocumentVersion.extracted_text at
    extraction time (app/documents/extractor.py), the same
    "verified-in-code-not-trusted" check app/ledger/extractor.py performs
    against the source chat message.

    `contradicts` is populated later, not by this session — Foresight
    (Prompt 7) is what *compares* spec claims across documents and links a
    conflicting pair; this milestone only extracts and stores claims (Prompt
    6's own EXPLICITLY OUT OF SCOPE note).
    """

    __tablename__ = "spec_claims"

    document_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document_versions.id"), index=True
    )
    deliverable_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("deliverables.id"), default=None
    )
    location_code: Mapped[str | None] = mapped_column(
        default=None, comment="position reference within a floor plan/render, e.g. Annex A's 'H', 'I'"
    )
    attribute: Mapped[str] = mapped_column(SpecClaimAttribute)
    value: Mapped[str] = mapped_column(comment="e.g. '2040mm x 1040mm', '1 set'")
    contradicts: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("spec_claims.id"), default=None
    )


DocumentAuditAction = Enum(
    "document_created", "version_created", "version_approved", "auto_tagged", "project_archived",
    name="document_audit_action",
)


class DocumentAuditLog(Base, UUIDPk):
    """The Document domain's own audit trail — a separate table from
    app/models/audit.py's AuditLog (commitment-scoped) and
    app/twin/models.py's TwinAuditLog, same "a second narrow table costs
    nothing that a wider polymorphic redesign would risk" reasoning
    TwinAuditLog's own docstring already gives for not generalising the
    ledger one.

    `project_archived` (FR-DOC-06) lives on this table rather than a new
    project-domain audit table of its own: it's this session's job to make
    "audited" mean something for that action at all (Prompt 6, item 9 — no
    prior session ever wrote to Project.archived_at), and it always fires
    alongside this table's other actions in the same request-handling code
    (app/documents/service.py's archive_project), never independently.

    Unlike TwinAuditLog's milestone_id/dependency_id (two independent,
    mutually-exclusive subjects, enforced by a CHECK), `document_id` and
    `document_version_id` here are parent/child, not alternatives — a
    `version_created`/`version_approved` event legitimately carries both
    (which document, which specific version), so there is no "at most one
    of these" constraint on this table.
    """

    __tablename__ = "document_audit_log"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), index=True, comment="RLS scope"
    )
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL"), default=None, index=True
    )
    document_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document_versions.id", ondelete="SET NULL"), default=None,
        index=True,
    )
    action: Mapped[str] = mapped_column(DocumentAuditAction)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), default=None
    )
    occurred_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
