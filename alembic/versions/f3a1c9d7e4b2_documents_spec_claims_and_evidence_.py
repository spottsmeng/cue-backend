"""documents, document_versions, spec_claims and evidence extension

Revision ID: f3a1c9d7e4b2
Revises: 9ddb100d7e8e
Create Date: 2026-08-07 21:00:00.000000

Prompt 6 (Documents, PRD §6.6/§4.3): Document/DocumentVersion/SpecClaim, the
evidence-required deferred triggers on DocumentVersion and SpecClaim (same
mechanism a895ae03ec5c already established for Commitment/Budget), RLS on
every new tenant-scoped table, and document_audit_log (this domain's own
append-only trail, same shape 616998fb7f1b's twin_audit_log established).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'f3a1c9d7e4b2'
down_revision: Union[str, Sequence[str], None] = '9ddb100d7e8e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # `vector` must exist before any table can declare a Vector(...) column
    # (DocumentVersion.embedding, below) — see docker-compose.yml's postgres
    # image change (pgvector/pgvector:pg17) for why the extension binary is
    # even available to CREATE.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector;")

    op.execute(
        "CREATE TYPE spec_claim_attribute AS ENUM ('dimension', 'finish', 'quantity', 'price');"
    )
    op.execute(
        "CREATE TYPE document_audit_action AS ENUM "
        "('document_created', 'version_created', 'version_approved', 'auto_tagged', "
        "'project_archived');"
    )

    # --- documents. `current_version_id` has no FK yet — document_versions
    # doesn't exist until the next op.create_table below, and the two tables
    # reference each other (a genuine circular dependency). The FK is added
    # by a separate op.create_foreign_key once both tables exist.
    op.create_table(
        'documents',
        sa.Column('project_id', sa.UUID(), nullable=False, comment='RLS scope'),
        sa.Column('name', sa.String(), nullable=False, comment='display title, e.g. original filename'),
        sa.Column('class_term_id', sa.UUID(), nullable=True, comment="ontology_terms row, category='deliverable_class'"),
        sa.Column('milestone_type_term_id', sa.UUID(), nullable=True, comment="ontology_terms row, category='milestone_type'"),
        sa.Column('phase_term_id', sa.UUID(), nullable=True, comment="ontology_terms row, category='phase'"),
        sa.Column('current_version_id', sa.UUID(), nullable=True, comment='FK to document_versions.id, added post-creation'),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['class_term_id'], ['ontology_terms.id'], ),
        sa.ForeignKeyConstraint(['milestone_type_term_id'], ['ontology_terms.id'], ),
        sa.ForeignKeyConstraint(['phase_term_id'], ['ontology_terms.id'], ),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_documents_project_id'), 'documents', ['project_id'], unique=False)

    # --- document_versions.
    op.create_table(
        'document_versions',
        sa.Column('document_id', sa.UUID(), nullable=False),
        sa.Column('version_no', sa.Integer(), nullable=False),
        sa.Column('storage_ref', sa.String(), nullable=False, comment='StorageBackend object key — original bytes, never mutated'),
        sa.Column('extracted_text', sa.String(), nullable=True, comment='caller-supplied stand-in for real OCR/parsing output'),
        sa.Column(
            'search_vector', postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', coalesce(extracted_text, ''))", persisted=True),
            nullable=False,
        ),
        sa.Column('embedding', Vector(1024), nullable=True, comment='FR-DOC-05 semantic search — unpopulated this session'),
        sa.Column('approved_by', sa.UUID(), nullable=True),
        sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['approved_by'], ['users.id'], ),
        sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('document_id', 'version_no', name='document_version_number_unique'),
    )
    op.create_index(op.f('ix_document_versions_document_id'), 'document_versions', ['document_id'], unique=False)
    op.create_index(
        'ix_document_versions_search_vector', 'document_versions', ['search_vector'], unique=False,
        postgresql_using='gin',
    )

    op.create_foreign_key(
        'documents_current_version_id_fkey', 'documents', 'document_versions',
        ['current_version_id'], ['id'],
    )

    # --- spec_claims.
    op.create_table(
        'spec_claims',
        sa.Column('document_version_id', sa.UUID(), nullable=False),
        sa.Column('deliverable_id', sa.UUID(), nullable=True),
        sa.Column('location_code', sa.String(), nullable=True, comment="position reference within a floor plan/render, e.g. Annex A's 'H', 'I'"),
        sa.Column('attribute', postgresql.ENUM('dimension', 'finish', 'quantity', 'price', name='spec_claim_attribute', create_type=False), nullable=False),
        sa.Column('value', sa.String(), nullable=False, comment="e.g. '2040mm x 1040mm', '1 set'"),
        sa.Column('contradicts', sa.UUID(), nullable=True),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(['contradicts'], ['spec_claims.id'], ),
        sa.ForeignKeyConstraint(['deliverable_id'], ['deliverables.id'], ),
        sa.ForeignKeyConstraint(['document_version_id'], ['document_versions.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_spec_claims_document_version_id'), 'spec_claims', ['document_version_id'], unique=False)

    # --- evidence extension (app/models/ledger.py's Evidence docstring):
    # two more nullable subject columns, and the exactly-one-subject CHECK
    # widened from 2 to 4 terms.
    op.add_column('evidence', sa.Column('document_version_id', sa.UUID(), nullable=True))
    op.add_column('evidence', sa.Column('spec_claim_id', sa.UUID(), nullable=True))
    op.create_index(op.f('ix_evidence_document_version_id'), 'evidence', ['document_version_id'], unique=False)
    op.create_index(op.f('ix_evidence_spec_claim_id'), 'evidence', ['spec_claim_id'], unique=False)
    op.create_foreign_key(
        'evidence_document_version_id_fkey', 'evidence', 'document_versions',
        ['document_version_id'], ['id'],
    )
    op.create_foreign_key(
        'evidence_spec_claim_id_fkey', 'evidence', 'spec_claims', ['spec_claim_id'], ['id'],
    )
    op.drop_constraint('evidence_exactly_one_subject', 'evidence', type_='check')
    op.create_check_constraint(
        'evidence_exactly_one_subject', 'evidence',
        "(commitment_id IS NOT NULL)::int + (budget_id IS NOT NULL)::int "
        "+ (document_version_id IS NOT NULL)::int + (spec_claim_id IS NOT NULL)::int = 1",
    )

    # --- Evidence-linkage enforcement on DocumentVersion/SpecClaim, same
    # deferred-constraint-trigger mechanism a895ae03ec5c established for
    # Commitment/Budget (see that migration's own comment for the mechanics
    # and why a column-level CHECK cannot express this).
    op.execute("""
        CREATE OR REPLACE FUNCTION check_document_version_has_evidence() RETURNS trigger AS $$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM evidence WHERE document_version_id = NEW.id) THEN
            RAISE EXCEPTION 'document_version % has no evidence (PRD §4.1, Prompt 6)', NEW.id;
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER document_version_requires_evidence
        AFTER INSERT OR UPDATE ON document_versions
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION check_document_version_has_evidence();
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION check_spec_claim_has_evidence() RETURNS trigger AS $$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM evidence WHERE spec_claim_id = NEW.id) THEN
            RAISE EXCEPTION 'spec_claim % has no evidence (PRD §4.3)', NEW.id;
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER spec_claim_requires_evidence
        AFTER INSERT OR UPDATE ON spec_claims
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION check_spec_claim_has_evidence();
    """)

    # --- document_audit_log (this domain's own append-only trail — see
    # app/documents/models.py's DocumentAuditLog docstring for why it's a
    # separate table from AuditLog/TwinAuditLog).
    op.create_table(
        'document_audit_log',
        sa.Column('project_id', sa.UUID(), nullable=False, comment='RLS scope'),
        sa.Column('document_id', sa.UUID(), nullable=True),
        sa.Column('document_version_id', sa.UUID(), nullable=True),
        sa.Column('action', postgresql.ENUM('document_created', 'version_created', 'version_approved', 'auto_tagged', 'project_archived', name='document_audit_action', create_type=False), nullable=False),
        sa.Column('actor_id', sa.UUID(), nullable=True),
        sa.Column('occurred_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('detail', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(['actor_id'], ['users.id'], ),
        sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['document_version_id'], ['document_versions.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_document_audit_log_project_id'), 'document_audit_log', ['project_id'], unique=False)
    op.create_index(op.f('ix_document_audit_log_document_id'), 'document_audit_log', ['document_id'], unique=False)
    op.create_index(op.f('ix_document_audit_log_document_version_id'), 'document_audit_log', ['document_version_id'], unique=False)

    op.execute("ALTER TABLE document_audit_log ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE document_audit_log FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY tenant_isolation ON document_audit_log
          USING (project_id IN (
            SELECT id FROM projects
            WHERE organisation_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
          ));
    """)

    # Append-only, with the SET NULL-on-delete carve-out built in from the
    # start (not a follow-up migration) — e771d0318751 already found the
    # hard way that an unconditional "reject every UPDATE" trigger blocks
    # the FK's own ON DELETE SET NULL, which Postgres implements as a real
    # UPDATE. Same fix, applied up front here.
    op.execute("""
        CREATE OR REPLACE FUNCTION forbid_document_audit_log_mutation() RETURNS trigger AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'document_audit_log is append-only: DELETE not permitted';
          END IF;
          IF NEW.project_id IS DISTINCT FROM OLD.project_id
             OR NEW.action IS DISTINCT FROM OLD.action
             OR NEW.actor_id IS DISTINCT FROM OLD.actor_id
             OR NEW.occurred_at IS DISTINCT FROM OLD.occurred_at
             OR NEW.detail IS DISTINCT FROM OLD.detail
             OR (NEW.document_id IS DISTINCT FROM OLD.document_id AND NEW.document_id IS NOT NULL)
             OR (NEW.document_version_id IS DISTINCT FROM OLD.document_version_id AND NEW.document_version_id IS NOT NULL)
          THEN
            RAISE EXCEPTION 'document_audit_log is append-only except SET NULL on delete: only document_id/document_version_id may be nulled';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER document_audit_log_append_only
        BEFORE UPDATE OR DELETE ON document_audit_log
        FOR EACH ROW EXECUTE FUNCTION forbid_document_audit_log_mutation();
    """)

    # --- RLS on documents/document_versions/spec_claims (CUE-Tech-Stack.md
    # P1). documents has a direct project_id column, same shape as
    # commitments/budgets/channels. document_versions and spec_claims have
    # none of their own — isolated via a join through documents (one level
    # and two levels respectively), same "policy expressed as a subquery
    # through the owning table" shape commitments' own policy already uses
    # for its project join.
    op.execute("ALTER TABLE documents ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE documents FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY tenant_isolation ON documents
          USING (project_id IN (
            SELECT id FROM projects
            WHERE organisation_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
          ));
    """)
    op.execute("ALTER TABLE document_versions ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE document_versions FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY tenant_isolation ON document_versions
          USING (document_id IN (
            SELECT id FROM documents WHERE project_id IN (
              SELECT id FROM projects
              WHERE organisation_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
            )
          ));
    """)
    op.execute("ALTER TABLE spec_claims ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE spec_claims FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY tenant_isolation ON spec_claims
          USING (document_version_id IN (
            SELECT dv.id FROM document_versions dv
            JOIN documents d ON d.id = dv.document_id
            JOIN projects p ON p.id = d.project_id
            WHERE p.organisation_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
          ));
    """)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON spec_claims;")
    op.execute("ALTER TABLE spec_claims DISABLE ROW LEVEL SECURITY;")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON document_versions;")
    op.execute("ALTER TABLE document_versions DISABLE ROW LEVEL SECURITY;")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON documents;")
    op.execute("ALTER TABLE documents DISABLE ROW LEVEL SECURITY;")

    op.execute("DROP TRIGGER IF EXISTS document_audit_log_append_only ON document_audit_log;")
    op.execute("DROP FUNCTION IF EXISTS forbid_document_audit_log_mutation();")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON document_audit_log;")
    op.execute("ALTER TABLE document_audit_log DISABLE ROW LEVEL SECURITY;")
    op.drop_index(op.f('ix_document_audit_log_document_version_id'), table_name='document_audit_log')
    op.drop_index(op.f('ix_document_audit_log_document_id'), table_name='document_audit_log')
    op.drop_index(op.f('ix_document_audit_log_project_id'), table_name='document_audit_log')
    op.drop_table('document_audit_log')

    op.execute("DROP TRIGGER IF EXISTS spec_claim_requires_evidence ON spec_claims;")
    op.execute("DROP FUNCTION IF EXISTS check_spec_claim_has_evidence();")
    op.execute("DROP TRIGGER IF EXISTS document_version_requires_evidence ON document_versions;")
    op.execute("DROP FUNCTION IF EXISTS check_document_version_has_evidence();")

    op.drop_constraint('evidence_exactly_one_subject', 'evidence', type_='check')
    op.create_check_constraint(
        'evidence_exactly_one_subject', 'evidence',
        "(commitment_id IS NOT NULL)::int + (budget_id IS NOT NULL)::int = 1",
    )
    op.drop_constraint('evidence_spec_claim_id_fkey', 'evidence', type_='foreignkey')
    op.drop_constraint('evidence_document_version_id_fkey', 'evidence', type_='foreignkey')
    op.drop_index(op.f('ix_evidence_spec_claim_id'), table_name='evidence')
    op.drop_index(op.f('ix_evidence_document_version_id'), table_name='evidence')
    op.drop_column('evidence', 'spec_claim_id')
    op.drop_column('evidence', 'document_version_id')

    op.drop_index(op.f('ix_spec_claims_document_version_id'), table_name='spec_claims')
    op.drop_table('spec_claims')

    op.drop_constraint('documents_current_version_id_fkey', 'documents', type_='foreignkey')

    op.drop_index('ix_document_versions_search_vector', table_name='document_versions')
    op.drop_index(op.f('ix_document_versions_document_id'), table_name='document_versions')
    op.drop_table('document_versions')

    op.drop_index(op.f('ix_documents_project_id'), table_name='documents')
    op.drop_table('documents')

    op.execute("DROP TYPE IF EXISTS document_audit_action;")
    op.execute("DROP TYPE IF EXISTS spec_claim_attribute;")
    # Dev-only downgrade path (never exercised in any shared/deployed
    # environment, same note a895ae03ec5c's own downgrade() makes) — leaves
    # the `vector` extension in place rather than dropping it, since another
    # table (none today, but conceivably added in the meantime) could depend
    # on it and DROP EXTENSION would be destructive beyond this migration's
    # own scope.
