"""twin: dependencies and milestone archetype templates

Revision ID: 616998fb7f1b
Revises: 83b72f2a9175
Create Date: 2026-08-07 16:34:11.902113

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '616998fb7f1b'
down_revision: Union[str, Sequence[str], None] = '83b72f2a9175'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # --- Reference data: the archetype template (see app/twin/models.py's
    # MilestoneArchetype/MilestoneArchetypeItem docstrings for why this is a
    # DB-backed template rather than a Python constant baked into
    # create_project). No organisation_id/project_id, no RLS — same posture
    # as ontology_terms/verticals, which are also global reference data
    # nothing in this schema scopes per-tenant.
    op.create_table(
        'milestone_archetypes',
        sa.Column('code', sa.String(), nullable=False, comment="stable key, e.g. 'event-production-default'"),
        sa.Column('vertical_id', sa.UUID(), nullable=True),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column(
            'is_default', sa.Boolean(), nullable=False,
            comment='which archetype create_project seeds from when none is specified',
        ),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(['vertical_id'], ['verticals.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code'),
    )
    op.create_index(
        op.f('ix_milestone_archetypes_vertical_id'), 'milestone_archetypes', ['vertical_id'],
        unique=False,
    )

    op.create_table(
        'milestone_archetype_items',
        sa.Column('archetype_id', sa.UUID(), nullable=False),
        sa.Column('sequence_order', sa.Integer(), nullable=False),
        sa.Column(
            'type_code', sa.String(), nullable=False,
            comment="ontology_terms.code, category='milestone_type'",
        ),
        sa.Column('name', sa.String(), nullable=False, comment='seeds Milestone.name at materialization'),
        sa.Column(
            'day_offset', sa.Integer(), nullable=False,
            comment="relative to the project's event_start; negative = before, 0 = event day",
        ),
        sa.Column(
            'is_fixed', sa.Boolean(), nullable=False,
            comment="FR-TWN-11: seeds Milestone.is_fixed, e.g. 'doors'",
        ),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(['archetype_id'], ['milestone_archetypes.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('archetype_id', 'sequence_order', name='archetype_item_sequence_unique'),
    )
    op.create_index(
        op.f('ix_milestone_archetype_items_archetype_id'), 'milestone_archetype_items', ['archetype_id'],
        unique=False,
    )

    # --- Operational data: per-project dependency edges (PRD §4.1: MILESTONE
    # ||--o{ DEPENDENCY). Lives here, not app/models/ledger.py — see that
    # file's own scope note and app/twin/models.py's module docstring.
    op.create_table(
        'dependencies',
        sa.Column('project_id', sa.UUID(), nullable=False, comment='RLS scope'),
        sa.Column('upstream_milestone_id', sa.UUID(), nullable=False),
        sa.Column('downstream_milestone_id', sa.UUID(), nullable=False),
        sa.Column('lag_days', sa.Integer(), nullable=False),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.CheckConstraint(
            'upstream_milestone_id != downstream_milestone_id', name='dependency_not_self'
        ),
        sa.CheckConstraint('lag_days >= 0', name='dependency_lag_days_non_negative'),
        sa.ForeignKeyConstraint(['downstream_milestone_id'], ['milestones.id'], ),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
        sa.ForeignKeyConstraint(['upstream_milestone_id'], ['milestones.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'upstream_milestone_id', 'downstream_milestone_id', name='dependency_edge_unique'
        ),
    )
    op.create_index(op.f('ix_dependencies_project_id'), 'dependencies', ['project_id'], unique=False)
    op.create_index(
        op.f('ix_dependencies_upstream_milestone_id'), 'dependencies', ['upstream_milestone_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_dependencies_downstream_milestone_id'), 'dependencies', ['downstream_milestone_id'],
        unique=False,
    )

    # --- `milestones` (a895ae03ec5c) never got a tenant_isolation policy —
    # it predates this session and had no endpoint reading or writing it yet
    # (app/models/ledger.py's own top-of-file note: Milestone "has never been
    # touched by any endpoint"), so the gap was latent rather than exploited.
    # This session is what activates it (app/api/milestones.py), and every
    # one of those endpoints happens to be safe regardless — they all filter
    # by a `project_id` that Depends(get_project)/require_project_role
    # already resolved through projects' own RLS + membership check first.
    # But 1b4a45ebc039's own comment on this exact posture ("an unprotected
    # table here is 'an unprotected hole in an otherwise-enforced model'")
    # applies just as much to a table this session is the first to put a
    # real query against, so close it here rather than leave it for whoever
    # writes the next Milestone-touching endpoint to discover. deliverables/
    # budgets are the same latent shape but still untouched by any endpoint
    # after this session too — left alone, same as Milestone was left alone
    # before now, for the same reason.
    op.execute("ALTER TABLE milestones ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE milestones FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY tenant_isolation ON milestones
          USING (project_id IN (
            SELECT id FROM projects
            WHERE organisation_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
          ));
    """)

    # --- Row-level tenant isolation, same project-join pattern as commitments/
    # audit_log/memberships/delegations (a895ae03ec5c / d05dce0f415d /
    # 1b4a45ebc039) — dependencies has no organisation_id column of its own.
    op.execute("ALTER TABLE dependencies ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE dependencies FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY tenant_isolation ON dependencies
          USING (project_id IN (
            SELECT id FROM projects
            WHERE organisation_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
          ));
    """)

    # --- Twin's own audit trail (FR-TWN-10's "recorded as a project-specific
    # adjustment, not a silent overwrite") — a separate table from audit_log,
    # not a generalisation of it; see app/twin/models.py's TwinAuditLog
    # docstring for why.
    op.create_table(
        'twin_audit_log',
        sa.Column('project_id', sa.UUID(), nullable=False, comment='RLS scope'),
        sa.Column('milestone_id', sa.UUID(), nullable=True),
        sa.Column('dependency_id', sa.UUID(), nullable=True),
        sa.Column(
            'action',
            sa.Enum('milestone_override', 'dependency_override', 'recompute', name='twin_audit_action'),
            nullable=False,
        ),
        sa.Column('actor_id', sa.UUID(), nullable=True),
        sa.Column(
            'occurred_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False,
        ),
        sa.Column('detail', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.CheckConstraint(
            'NOT (milestone_id IS NOT NULL AND dependency_id IS NOT NULL)',
            name='twin_audit_log_not_both_subjects',
        ),
        sa.ForeignKeyConstraint(['actor_id'], ['users.id'], ),
        sa.ForeignKeyConstraint(['dependency_id'], ['dependencies.id'], ),
        sa.ForeignKeyConstraint(['milestone_id'], ['milestones.id'], ),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_twin_audit_log_project_id'), 'twin_audit_log', ['project_id'], unique=False)
    op.create_index(
        op.f('ix_twin_audit_log_milestone_id'), 'twin_audit_log', ['milestone_id'], unique=False
    )
    op.create_index(
        op.f('ix_twin_audit_log_dependency_id'), 'twin_audit_log', ['dependency_id'], unique=False
    )

    op.execute("ALTER TABLE twin_audit_log ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE twin_audit_log FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY tenant_isolation ON twin_audit_log
          USING (project_id IN (
            SELECT id FROM projects
            WHERE organisation_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
          ));
    """)

    # --- FR-TWN-10 / append-only posture, same as audit_log
    # (d05dce0f415d's forbid_audit_log_mutation) — a Twin override is a new
    # row, never an edit to a prior one.
    op.execute("""
        CREATE OR REPLACE FUNCTION forbid_twin_audit_log_mutation() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'twin_audit_log is append-only (FR-TWN-10): % not permitted', TG_OP;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER twin_audit_log_append_only
        BEFORE UPDATE OR DELETE ON twin_audit_log
        FOR EACH ROW EXECUTE FUNCTION forbid_twin_audit_log_mutation();
    """)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP TRIGGER IF EXISTS twin_audit_log_append_only ON twin_audit_log;")
    op.execute("DROP FUNCTION IF EXISTS forbid_twin_audit_log_mutation();")

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON twin_audit_log;")
    op.execute("ALTER TABLE twin_audit_log DISABLE ROW LEVEL SECURITY;")
    op.drop_index(op.f('ix_twin_audit_log_dependency_id'), table_name='twin_audit_log')
    op.drop_index(op.f('ix_twin_audit_log_milestone_id'), table_name='twin_audit_log')
    op.drop_index(op.f('ix_twin_audit_log_project_id'), table_name='twin_audit_log')
    op.drop_table('twin_audit_log')
    op.execute("DROP TYPE IF EXISTS twin_audit_action;")

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON milestones;")
    op.execute("ALTER TABLE milestones DISABLE ROW LEVEL SECURITY;")

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON dependencies;")
    op.execute("ALTER TABLE dependencies DISABLE ROW LEVEL SECURITY;")
    op.drop_index(op.f('ix_dependencies_downstream_milestone_id'), table_name='dependencies')
    op.drop_index(op.f('ix_dependencies_upstream_milestone_id'), table_name='dependencies')
    op.drop_index(op.f('ix_dependencies_project_id'), table_name='dependencies')
    op.drop_table('dependencies')

    op.drop_index(op.f('ix_milestone_archetype_items_archetype_id'), table_name='milestone_archetype_items')
    op.drop_table('milestone_archetype_items')

    op.drop_index(op.f('ix_milestone_archetypes_vertical_id'), table_name='milestone_archetypes')
    op.drop_table('milestone_archetypes')
