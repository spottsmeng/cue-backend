"""ask retrieval index and conversations

Revision ID: c7a2f4d8e1b5
Revises: a1ba98d78fee
Create Date: 2026-08-08 06:00:00.000000

Prompt 9 (Ask & retrieval, PRD §6.11 FR-ASK): retrieval_chunks (the hybrid
lexical+semantic index for Evidence/AuditLog text — DocumentVersion already
has its own embedding/search_vector columns from the Documents session, left
unpopulated on purpose for this one to fill), ask_conversations and ask_turns
(FR-ASK-08's follow-up/session state). RLS on every new tenant-scoped table,
same tenant_isolation policy shape every other domain's migration already
establishes. `vector` extension already exists (created by
f3a1c9d7e4b2) — no need to CREATE EXTENSION again.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c7a2f4d8e1b5'
down_revision: Union[str, Sequence[str], None] = 'a1ba98d78fee'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("CREATE TYPE retrieval_source_type AS ENUM ('evidence', 'audit_log');")

    # --- retrieval_chunks.
    op.create_table(
        'retrieval_chunks',
        sa.Column('project_id', sa.UUID(), nullable=False, comment='RLS scope'),
        sa.Column('source_type', postgresql.ENUM('evidence', 'audit_log', name='retrieval_source_type', create_type=False), nullable=False),
        sa.Column('source_id', sa.UUID(), nullable=False),
        sa.Column('text', sa.String(), nullable=False, comment='the text that was embedded/indexed'),
        sa.Column(
            'search_vector', postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', coalesce(text, ''))", persisted=True),
            nullable=False,
        ),
        sa.Column('embedding', Vector(1024), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_retrieval_chunks_project_id'), 'retrieval_chunks', ['project_id'], unique=False)
    op.create_index(
        'ix_retrieval_chunks_source', 'retrieval_chunks', ['source_type', 'source_id'], unique=True,
    )
    op.create_index(
        'ix_retrieval_chunks_search_vector', 'retrieval_chunks', ['search_vector'], unique=False,
        postgresql_using='gin',
    )

    op.execute("ALTER TABLE retrieval_chunks ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE retrieval_chunks FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY tenant_isolation ON retrieval_chunks
          USING (project_id IN (
            SELECT id FROM projects
            WHERE organisation_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
          ));
    """)

    # --- ask_conversations (FR-ASK-08).
    op.create_table(
        'ask_conversations',
        sa.Column('project_id', sa.UUID(), nullable=False, comment='RLS scope'),
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_ask_conversations_project_id'), 'ask_conversations', ['project_id'], unique=False)
    op.create_index(op.f('ix_ask_conversations_user_id'), 'ask_conversations', ['user_id'], unique=False)

    op.execute("ALTER TABLE ask_conversations ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE ask_conversations FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY tenant_isolation ON ask_conversations
          USING (project_id IN (
            SELECT id FROM projects
            WHERE organisation_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
          ));
    """)

    # --- ask_turns.
    op.create_table(
        'ask_turns',
        sa.Column('conversation_id', sa.UUID(), nullable=False),
        sa.Column('project_id', sa.UUID(), nullable=False, comment='RLS scope'),
        sa.Column('question', sa.String(), nullable=False),
        sa.Column('answer_available', sa.Boolean(), nullable=False),
        sa.Column('answer_text', sa.String(), nullable=True),
        sa.Column('citation_source_types', postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column('citation_source_ids', postgresql.ARRAY(sa.UUID()), nullable=False),
        sa.Column('asked_by', sa.UUID(), nullable=False),
        sa.Column('asked_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(['asked_by'], ['users.id'], ),
        sa.ForeignKeyConstraint(['conversation_id'], ['ask_conversations.id'], ),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_ask_turns_conversation_id'), 'ask_turns', ['conversation_id'], unique=False)
    op.create_index(op.f('ix_ask_turns_project_id'), 'ask_turns', ['project_id'], unique=False)

    op.execute("ALTER TABLE ask_turns ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE ask_turns FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY tenant_isolation ON ask_turns
          USING (project_id IN (
            SELECT id FROM projects
            WHERE organisation_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
          ));
    """)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON ask_turns;")
    op.execute("ALTER TABLE ask_turns DISABLE ROW LEVEL SECURITY;")
    op.drop_index(op.f('ix_ask_turns_project_id'), table_name='ask_turns')
    op.drop_index(op.f('ix_ask_turns_conversation_id'), table_name='ask_turns')
    op.drop_table('ask_turns')

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON ask_conversations;")
    op.execute("ALTER TABLE ask_conversations DISABLE ROW LEVEL SECURITY;")
    op.drop_index(op.f('ix_ask_conversations_user_id'), table_name='ask_conversations')
    op.drop_index(op.f('ix_ask_conversations_project_id'), table_name='ask_conversations')
    op.drop_table('ask_conversations')

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON retrieval_chunks;")
    op.execute("ALTER TABLE retrieval_chunks DISABLE ROW LEVEL SECURITY;")
    op.drop_index('ix_retrieval_chunks_search_vector', table_name='retrieval_chunks')
    op.drop_index('ix_retrieval_chunks_source', table_name='retrieval_chunks')
    op.drop_index(op.f('ix_retrieval_chunks_project_id'), table_name='retrieval_chunks')
    op.drop_table('retrieval_chunks')

    op.execute("DROP TYPE IF EXISTS retrieval_source_type;")
