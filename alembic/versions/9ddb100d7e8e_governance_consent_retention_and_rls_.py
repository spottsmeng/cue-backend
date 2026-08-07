"""governance: consent, retention, and RLS closure on budgets/channels

Revision ID: 9ddb100d7e8e
Revises: e771d0318751
Create Date: 2026-08-07 20:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '9ddb100d7e8e'
down_revision: Union[str, Sequence[str], None] = 'e771d0318751'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # --- FR-LED-13: payment_status is Finance-role-set, structurally
    # distinct from the general /verify correction path (app/api/
    # commitments.py's update_payment_status) — it needs its own audit
    # action value so the trail records *which* path made the change, same
    # "created"/"verified"/"corrected"/"state_transition" distinction
    # audit_action already draws. One ADD VALUE per statement, same
    # constraint d7def2e27c7c's twin_audit_action additions already document.
    op.execute("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'payment_status_updated';")

    # --- FR-ADM-07 / PRD §4.1 PARTY ||--o{ CONSENT_RECORD.
    op.create_table(
        'consent_records',
        sa.Column('party_id', sa.UUID(), nullable=False),
        sa.Column('project_id', sa.UUID(), nullable=False, comment='RLS scope'),
        sa.Column('notice_sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'status',
            sa.Enum('pending', 'accepted', 'objected', 'opted_out', name='consent_status'),
            nullable=False,
        ),
        sa.Column('evidence', sa.String(), nullable=True),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['party_id'], ['parties.id'], ),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('party_id', 'project_id', name='consent_records_party_project_key'),
    )
    op.create_index(op.f('ix_consent_records_party_id'), 'consent_records', ['party_id'], unique=False)
    op.create_index(op.f('ix_consent_records_project_id'), 'consent_records', ['project_id'], unique=False)

    op.execute("ALTER TABLE consent_records ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE consent_records FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY tenant_isolation ON consent_records
          USING (project_id IN (
            SELECT id FROM projects
            WHERE organisation_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
          ));
    """)

    # --- FR-ADM-08. organisation_id is a direct column (org-wide config, not
    # project-scoped) — same tenant_isolation shape as `users`, not the
    # project-join shape consent_records/budgets/channels need.
    op.create_table(
        'retention_policies',
        sa.Column('organisation_id', sa.UUID(), nullable=False, comment='RLS scope'),
        sa.Column('vertical_id', sa.UUID(), nullable=True, comment="'project type' tier; NULL applies org-wide across every vertical"),
        sa.Column('region', sa.String(), nullable=True, comment='NULL applies to every region'),
        sa.Column('retention_days', sa.Integer(), nullable=False),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint('retention_days > 0', name='retention_policy_positive_window'),
        sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ),
        sa.ForeignKeyConstraint(['vertical_id'], ['verticals.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_retention_policies_organisation_id'), 'retention_policies', ['organisation_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_retention_policies_vertical_id'), 'retention_policies', ['vertical_id'], unique=False
    )

    op.execute("ALTER TABLE retention_policies ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE retention_policies FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY tenant_isolation ON retention_policies
          USING (organisation_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid);
    """)

    # --- `budgets`/`channels` (a895ae03ec5c) never got a tenant_isolation
    # policy — that migration's own comment says the RLS pattern was
    # "demonstrated on organisations, projects and commitments... extend the
    # same pattern to remaining tenant-owned tables as they're added; not
    # done exhaustively here", and 616998fb7f1b's milestones comment records
    # the rule this session follows: the gap is latent until a real endpoint
    # is the first to query the table, at which point closing it is this
    # session's job, not left for whoever writes that endpoint next. This
    # session is what activates both (app/api/budget.py, app/api/
    # channels.py) — same project-join pattern as commitments/dependencies,
    # since neither table has an organisation_id column of its own.
    op.execute("ALTER TABLE budgets ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE budgets FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY tenant_isolation ON budgets
          USING (project_id IN (
            SELECT id FROM projects
            WHERE organisation_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
          ));
    """)
    op.execute("ALTER TABLE channels ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE channels FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY tenant_isolation ON channels
          USING (project_id IN (
            SELECT id FROM projects
            WHERE organisation_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
          ));
    """)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON channels;")
    op.execute("ALTER TABLE channels DISABLE ROW LEVEL SECURITY;")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON budgets;")
    op.execute("ALTER TABLE budgets DISABLE ROW LEVEL SECURITY;")

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON retention_policies;")
    op.execute("ALTER TABLE retention_policies DISABLE ROW LEVEL SECURITY;")
    op.drop_index(op.f('ix_retention_policies_vertical_id'), table_name='retention_policies')
    op.drop_index(op.f('ix_retention_policies_organisation_id'), table_name='retention_policies')
    op.drop_table('retention_policies')

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON consent_records;")
    op.execute("ALTER TABLE consent_records DISABLE ROW LEVEL SECURITY;")
    op.drop_index(op.f('ix_consent_records_project_id'), table_name='consent_records')
    op.drop_index(op.f('ix_consent_records_party_id'), table_name='consent_records')
    op.drop_table('consent_records')
    op.execute("DROP TYPE IF EXISTS consent_status;")

    # Postgres has no DROP VALUE for enum types — same accepted, documented
    # limitation d7def2e27c7c's downgrade() notes for twin_audit_action.
    # Dev-only downgrade path; not worth a type rebuild.
