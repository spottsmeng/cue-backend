"""writeback: outbound_messages, writeback_audit_log, projects ceiling, audit_action value

Revision ID: 9197d521030d
Revises: c9f2a6e5d1b7
Create Date: 2026-08-09 10:00:00.000000

Prompt 12 (PRD §6.12, FR-WBK-01 through 08): OutboundMessage (the draft ->
authorise -> send cycle, one row per write-back attempt, always tied to the
commitment decision it confirms), this domain's own WritebackAuditLog (the
rate ceiling's own "explicit, audited configuration change" requirement —
FR-WBK-04 — has no commitment to hang an AuditLog row off of), a new
writeback_daily_ceiling column on projects (FR-WBK-04's per-project
configurable ceiling), and a new audit_action value for the shared
commitment audit trail every real send is logged through (FR-WBK-08, per
Prompt 12's own instruction: extend the enum, don't overload "corrected").
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '9197d521030d'
down_revision: Union[str, Sequence[str], None] = 'c9f2a6e5d1b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- projects.writeback_daily_ceiling
    op.add_column(
        'projects',
        sa.Column(
            'writeback_daily_ceiling', sa.Integer(), nullable=False, server_default='1',
            comment="FR-WBK-04's hard ceiling — outbound messages per group per local calendar day",
        ),
    )

    # --- shared audit_action: a new value for FR-WBK-08's "log every send",
    # same ADD VALUE pattern d7def2e27c7c/9ddb100d7e8e already established.
    op.execute("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'outbound_sent';")

    # --- outbound_messages
    op.execute("CREATE TYPE outbound_message_status AS ENUM ('draft', 'authorised', 'sent');")
    op.execute(
        "CREATE TYPE outbound_message_reply_outcome AS ENUM ('transitioned', 'escalated');"
    )

    op.create_table(
        'outbound_messages',
        sa.Column('project_id', sa.UUID(), nullable=False, comment='RLS scope'),
        sa.Column('channel_id', sa.UUID(), nullable=False, comment="FR-WBK-01: originating vendor group"),
        sa.Column('commitment_id', sa.UUID(), nullable=False, comment='the decision this message confirms'),
        sa.Column('party_id', sa.UUID(), nullable=False, comment='the vendor being addressed'),
        sa.Column('to_external_id', sa.String(), nullable=False),
        sa.Column('language', sa.String(), nullable=False, comment="FR-WBK-02"),
        sa.Column('draft_text', sa.String(), nullable=False, comment="FR-WBK-03"),
        sa.Column(
            'status',
            postgresql.ENUM('draft', 'authorised', 'sent', name='outbound_message_status', create_type=False),
            nullable=False,
        ),
        sa.Column('authorised_by', sa.UUID(), nullable=True),
        sa.Column('authorised_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('rate_limit_bucket', sa.String(), nullable=True),
        sa.Column('reply_message_id', sa.UUID(), nullable=True),
        sa.Column(
            'reply_outcome',
            postgresql.ENUM(
                'transitioned', 'escalated', name='outbound_message_reply_outcome', create_type=False
            ),
            nullable=True,
        ),
        sa.Column('reply_detail', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['authorised_by'], ['users.id'], ),
        sa.ForeignKeyConstraint(['channel_id'], ['channels.id'], ),
        sa.ForeignKeyConstraint(['commitment_id'], ['commitments.id'], ),
        sa.ForeignKeyConstraint(['party_id'], ['parties.id'], ),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
        sa.ForeignKeyConstraint(['reply_message_id'], ['messages.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint(
            "(status = 'draft' AND authorised_by IS NULL AND authorised_at IS NULL AND sent_at IS NULL) "
            "OR (status = 'authorised' AND authorised_by IS NOT NULL AND authorised_at IS NOT NULL "
            "AND sent_at IS NULL) "
            "OR (status = 'sent' AND authorised_by IS NOT NULL AND authorised_at IS NOT NULL "
            "AND sent_at IS NOT NULL)",
            name='outbound_message_status_field_consistency',
        ),
    )
    op.create_index(op.f('ix_outbound_messages_project_id'), 'outbound_messages', ['project_id'], unique=False)
    op.create_index(op.f('ix_outbound_messages_channel_id'), 'outbound_messages', ['channel_id'], unique=False)
    op.create_index(
        op.f('ix_outbound_messages_commitment_id'), 'outbound_messages', ['commitment_id'], unique=False
    )
    op.create_index(
        op.f('ix_outbound_messages_reply_message_id'), 'outbound_messages', ['reply_message_id'], unique=False
    )
    op.create_index(
        op.f('ix_outbound_messages_rate_limit_bucket'), 'outbound_messages', ['rate_limit_bucket'], unique=False
    )

    op.execute("ALTER TABLE outbound_messages ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE outbound_messages FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY tenant_isolation ON outbound_messages
          USING (project_id IN (
            SELECT id FROM projects
            WHERE organisation_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
          ));
    """)

    # --- writeback_audit_log (this domain's own append-only trail — see
    # app/writeback/models.py's WritebackAuditLog docstring for why this
    # exists separately from the shared, commitment-scoped audit_log).
    op.execute("CREATE TYPE writeback_audit_action AS ENUM ('config_updated');")
    op.create_table(
        'writeback_audit_log',
        sa.Column('project_id', sa.UUID(), nullable=False, comment='RLS scope'),
        sa.Column(
            'action',
            postgresql.ENUM('config_updated', name='writeback_audit_action', create_type=False),
            nullable=False,
        ),
        sa.Column('actor_id', sa.UUID(), nullable=True),
        sa.Column('occurred_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('detail', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(['actor_id'], ['users.id'], ),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_writeback_audit_log_project_id'), 'writeback_audit_log', ['project_id'], unique=False
    )

    op.execute("ALTER TABLE writeback_audit_log ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE writeback_audit_log FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY tenant_isolation ON writeback_audit_log
          USING (project_id IN (
            SELECT id FROM projects
            WHERE organisation_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
          ));
    """)
    # Append-only — no SET-NULL-on-delete carve-out needed (no other table
    # holds a FK into this one), unlike foresight_audit_log/document_audit_log.
    op.execute("""
        CREATE OR REPLACE FUNCTION forbid_writeback_audit_log_mutation() RETURNS trigger AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'writeback_audit_log is append-only: DELETE not permitted';
          END IF;
          RAISE EXCEPTION 'writeback_audit_log is append-only: UPDATE not permitted';
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER writeback_audit_log_append_only
        BEFORE UPDATE OR DELETE ON writeback_audit_log
        FOR EACH ROW EXECUTE FUNCTION forbid_writeback_audit_log_mutation();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS writeback_audit_log_append_only ON writeback_audit_log;")
    op.execute("DROP FUNCTION IF EXISTS forbid_writeback_audit_log_mutation();")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON writeback_audit_log;")
    op.execute("ALTER TABLE writeback_audit_log DISABLE ROW LEVEL SECURITY;")
    op.drop_index(op.f('ix_writeback_audit_log_project_id'), table_name='writeback_audit_log')
    op.drop_table('writeback_audit_log')
    op.execute("DROP TYPE IF EXISTS writeback_audit_action;")

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON outbound_messages;")
    op.execute("ALTER TABLE outbound_messages DISABLE ROW LEVEL SECURITY;")
    op.drop_index(op.f('ix_outbound_messages_rate_limit_bucket'), table_name='outbound_messages')
    op.drop_index(op.f('ix_outbound_messages_reply_message_id'), table_name='outbound_messages')
    op.drop_index(op.f('ix_outbound_messages_commitment_id'), table_name='outbound_messages')
    op.drop_index(op.f('ix_outbound_messages_channel_id'), table_name='outbound_messages')
    op.drop_index(op.f('ix_outbound_messages_project_id'), table_name='outbound_messages')
    op.drop_table('outbound_messages')
    op.execute("DROP TYPE IF EXISTS outbound_message_reply_outcome;")
    op.execute("DROP TYPE IF EXISTS outbound_message_status;")

    # Postgres has no DROP VALUE for enum types — same accepted, documented
    # limitation d7def2e27c7c's downgrade() notes. Dev-only downgrade path.

    op.drop_column('projects', 'writeback_daily_ceiling')
