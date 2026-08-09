"""channel extraction schedules

Revision ID: c9f2a6e5d1b7
Revises: b6e4a1c9f235
Create Date: 2026-08-08 22:00:00.000000

Prompt 11 item 11 (FR-CAP-10, Should): scheduled extraction windows,
configurable per channel — app/capture/models.py's ChannelExtractionSchedule,
app/capture/schedule.py's arq-scheduled reader.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c9f2a6e5d1b7'
down_revision: Union[str, Sequence[str], None] = 'b6e4a1c9f235'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'channel_extraction_schedules',
        sa.Column('project_id', sa.UUID(), nullable=False, comment='RLS scope'),
        sa.Column('channel_id', sa.UUID(), nullable=False),
        sa.Column('interval_minutes', sa.Integer(), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('last_run_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_by', sa.UUID(), nullable=False),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint('interval_minutes > 0', name='channel_extraction_schedules_positive_interval'),
        sa.ForeignKeyConstraint(['channel_id'], ['channels.id'], ),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('channel_id', name='channel_extraction_schedules_one_per_channel'),
    )
    op.create_index(
        op.f('ix_channel_extraction_schedules_project_id'),
        'channel_extraction_schedules', ['project_id'], unique=False,
    )
    op.create_index(
        op.f('ix_channel_extraction_schedules_channel_id'),
        'channel_extraction_schedules', ['channel_id'], unique=False,
    )

    op.execute("ALTER TABLE channel_extraction_schedules ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE channel_extraction_schedules FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY tenant_isolation ON channel_extraction_schedules
          USING (project_id IN (
            SELECT id FROM projects
            WHERE organisation_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
          ));
    """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON channel_extraction_schedules;")
    op.execute("ALTER TABLE channel_extraction_schedules DISABLE ROW LEVEL SECURITY;")
    op.drop_index(
        op.f('ix_channel_extraction_schedules_channel_id'), table_name='channel_extraction_schedules'
    )
    op.drop_index(
        op.f('ix_channel_extraction_schedules_project_id'), table_name='channel_extraction_schedules'
    )
    op.drop_table('channel_extraction_schedules')
