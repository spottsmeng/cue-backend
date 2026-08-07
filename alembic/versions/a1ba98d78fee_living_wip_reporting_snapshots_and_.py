"""living wip reporting: snapshots and schedule config

Revision ID: a1ba98d78fee
Revises: ab0dc47865c9
Create Date: 2026-08-08 05:00:00.000000

Prompt 8 (Living WIP and reporting, PRD §6.10 FR-RPT): report_snapshots
(FR-RPT-08's immutable export artefact — a real, DB-enforced append-only
table, no UPDATE or DELETE permitted at all, not just omitted application
code) and report_schedule_configs (FR-RPT-09/10's config surface). The
"current" report itself is computed fresh on every read
(app/reports/composer.py) and needs no table of its own — see
app/reports/models.py's module docstring for why only these two need one.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a1ba98d78fee'
down_revision: Union[str, Sequence[str], None] = 'ab0dc47865c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("CREATE TYPE report_format AS ENUM ('pptx', 'pdf');")
    op.execute("CREATE TYPE report_trigger AS ENUM ('manual', 'scheduled');")

    # --- report_snapshots (FR-RPT-08).
    op.create_table(
        'report_snapshots',
        sa.Column('project_id', sa.UUID(), nullable=False, comment='RLS scope'),
        sa.Column('format', postgresql.ENUM('pptx', 'pdf', name='report_format', create_type=False), nullable=False),
        sa.Column('template_code', sa.String(), nullable=False, comment='app/reports/templates.py registry key'),
        sa.Column('storage_ref', sa.String(), nullable=False, comment='StorageBackend object key for the rendered file'),
        sa.Column('report_json', postgresql.JSONB(astext_type=sa.Text()), nullable=False, comment='LivingWipReportOut as composed at export time'),
        sa.Column('trigger', postgresql.ENUM('manual', 'scheduled', name='report_trigger', create_type=False), nullable=False),
        sa.Column('generated_by', sa.UUID(), nullable=True, comment='null for a scheduled/system-triggered export'),
        sa.Column('generated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(['generated_by'], ['users.id'], ),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_report_snapshots_project_id'), 'report_snapshots', ['project_id'], unique=False)

    op.execute("ALTER TABLE report_snapshots ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE report_snapshots FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY tenant_isolation ON report_snapshots
          USING (project_id IN (
            SELECT id FROM projects
            WHERE organisation_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
          ));
    """)

    # Fully immutable — no legitimate UPDATE at all (unlike twin_audit_log/
    # document_audit_log/foresight_audit_log, nothing else holds a FK into
    # this table that would ever need a SET-NULL carve-out), per FR-RPT-08:
    # "never mutated after creation." Enforced in the DB, not just by
    # omitting update/delete code paths — CLAUDE.md's "verified in code, not
    # trusted" posture, applied here the same way the evidence and audit_log
    # triggers already apply it elsewhere in this schema.
    op.execute("""
        CREATE OR REPLACE FUNCTION forbid_report_snapshot_mutation() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'report_snapshots is immutable: % not permitted (FR-RPT-08)', TG_OP;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER report_snapshots_immutable
        BEFORE UPDATE OR DELETE ON report_snapshots
        FOR EACH ROW EXECUTE FUNCTION forbid_report_snapshot_mutation();
    """)

    # --- report_schedule_configs (FR-RPT-09/10).
    op.create_table(
        'report_schedule_configs',
        sa.Column('project_id', sa.UUID(), nullable=False, comment='RLS scope'),
        sa.Column('day_of_week', sa.Integer(), nullable=False, comment='0=Monday .. 6=Sunday, local to Project.timezone'),
        sa.Column('hour_local', sa.Integer(), nullable=False),
        sa.Column('minute_local', sa.Integer(), nullable=False),
        sa.Column('format', postgresql.ENUM('pptx', 'pdf', name='report_format', create_type=False), nullable=False),
        sa.Column('template_code', sa.String(), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('last_run_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_by', sa.UUID(), nullable=False),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint('day_of_week >= 0 AND day_of_week <= 6', name='report_schedule_day_of_week_range'),
        sa.CheckConstraint('hour_local >= 0 AND hour_local <= 23', name='report_schedule_hour_local_range'),
        sa.CheckConstraint('minute_local >= 0 AND minute_local <= 59', name='report_schedule_minute_local_range'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_report_schedule_configs_project_id'), 'report_schedule_configs', ['project_id'], unique=False)

    op.execute("ALTER TABLE report_schedule_configs ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE report_schedule_configs FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY tenant_isolation ON report_schedule_configs
          USING (project_id IN (
            SELECT id FROM projects
            WHERE organisation_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
          ));
    """)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON report_schedule_configs;")
    op.execute("ALTER TABLE report_schedule_configs DISABLE ROW LEVEL SECURITY;")
    op.drop_index(op.f('ix_report_schedule_configs_project_id'), table_name='report_schedule_configs')
    op.drop_table('report_schedule_configs')

    op.execute("DROP TRIGGER IF EXISTS report_snapshots_immutable ON report_snapshots;")
    op.execute("DROP FUNCTION IF EXISTS forbid_report_snapshot_mutation();")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON report_snapshots;")
    op.execute("ALTER TABLE report_snapshots DISABLE ROW LEVEL SECURITY;")
    op.drop_index(op.f('ix_report_snapshots_project_id'), table_name='report_snapshots')
    op.drop_table('report_snapshots')

    op.execute("DROP TYPE IF EXISTS report_trigger;")
    op.execute("DROP TYPE IF EXISTS report_format;")
