"""add channel_types reference table

Revision ID: c3f6a2b9d417
Revises: 09bcd1591445
Create Date: 2026-08-08 16:00:00.000000

Retires the native `channel_type` Postgres enum (app/models/project.py) in
favour of insertable reference data — see app/models/channel_type.py's own
docstring and CUE-Tech-Stack.md §5.2 for why. This migration only creates
the table and its RLS policy; seeding and the FK conversion of
channels.type / channel_identities.channel_type / evidence.channel are
separate migrations (one seed-data change, one schema change, kept apart
same as ontology_terms' own create/seed migrations were).

`organisation_id IS NULL OR organisation_id = current_org_id` is the policy
shape ontology_terms itself is missing (no RLS policy exists on it at all,
confirmed by inspection) — universal/platform-shipped rows (organisation_id
NULL) must stay visible to every tenant, not just their own.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c3f6a2b9d417'
down_revision: Union[str, Sequence[str], None] = '09bcd1591445'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'channel_types',
        sa.Column('code', sa.String(), nullable=False, comment='stable machine key, never renamed once referenced'),
        sa.Column('capability', sa.String(), nullable=True),
        sa.Column('organisation_id', sa.UUID(), nullable=True, comment='NULL = platform-shipped'),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint(
            "capability IS NULL OR capability IN "
            "('external_vendor_chat', 'team_collaboration', 'email', 'file_storage')",
            name='channel_types_capability_known_value',
        ),
        sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code', name='channel_types_code_key'),
    )
    op.create_index(
        op.f('ix_channel_types_organisation_id'), 'channel_types', ['organisation_id'], unique=False
    )

    op.execute("ALTER TABLE channel_types ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE channel_types FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY tenant_isolation ON channel_types
          USING (
            organisation_id IS NULL
            OR organisation_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
          );
    """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON channel_types;")
    op.execute("ALTER TABLE channel_types DISABLE ROW LEVEL SECURITY;")
    op.drop_index(op.f('ix_channel_types_organisation_id'), table_name='channel_types')
    op.drop_table('channel_types')
