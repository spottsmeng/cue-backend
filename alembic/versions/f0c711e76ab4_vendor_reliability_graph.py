"""vendor reliability graph: vendor_metrics, party/project segmentation columns

Revision ID: f0c711e76ab4
Revises: c7a2f4d8e1b5
Create Date: 2026-08-08 05:00:00.000000

Prompt 10 (CUE-PRD.md §6.13, FR-VRG-01 through 07): the vendor_metrics table
(app/parties/models.py's VendorMetric — org-scoped RLS, same direct-column
shape as `organisations`/`foresight_thresholds`, not the project-join shape
most tenant-scoped tables use, since Party itself is org-scoped, not
project-scoped), plus the two FR-VRG-02 segmentation columns this session
found were missing: `parties.vendor_category_term_id` (the `vendor_category`
ontology_terms category, named since the first migration but never wired to
an actual column until now) / `parties.city`, and `projects.archetype_code`
(the only place a project's resolved MilestoneArchetype survives past
`materialize_archetype` — see app/models/project.py's own column comment).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'f0c711e76ab4'
down_revision: Union[str, Sequence[str], None] = 'c7a2f4d8e1b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        "CREATE TYPE vendor_metric_name AS ENUM "
        "('median_response_time_days', 'on_time_rate', 'revision_churn', "
        "'price_drift_pct', 'deviation_frequency');"
    )

    # --- FR-VRG-02 segmentation columns on tables that already exist.
    op.add_column('parties', sa.Column('vendor_category_term_id', sa.UUID(), nullable=True, comment="ontology_terms row, category='vendor_category'"))
    op.add_column('parties', sa.Column('city', sa.String(), nullable=True))
    op.create_index(op.f('ix_parties_vendor_category_term_id'), 'parties', ['vendor_category_term_id'], unique=False)
    op.create_foreign_key(
        'parties_vendor_category_term_id_fkey', 'parties', 'ontology_terms',
        ['vendor_category_term_id'], ['id'],
    )

    op.add_column(
        'projects',
        sa.Column(
            'archetype_code', sa.String(), nullable=True,
            comment='FR-VRG-02 event-archetype segmentation axis; set once at materialize_archetype time',
        ),
    )

    # --- vendor_metrics (app/parties/models.py's VendorMetric).
    op.create_table(
        'vendor_metrics',
        sa.Column('organisation_id', sa.UUID(), nullable=False, comment='RLS scope'),
        sa.Column('party_id', sa.UUID(), nullable=False),
        sa.Column('metric', postgresql.ENUM('median_response_time_days', 'on_time_rate', 'revision_churn', 'price_drift_pct', 'deviation_frequency', name='vendor_metric_name', create_type=False), nullable=False),
        sa.Column('value', sa.Float(), nullable=True),
        sa.Column('sample_size', sa.Integer(), nullable=False),
        sa.Column('unavailable_reason', sa.String(), nullable=True),
        sa.Column('segment_vendor_category', sa.String(), nullable=True),
        sa.Column('segment_city', sa.String(), nullable=True),
        sa.Column('segment_event_archetype', sa.String(), nullable=True),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('computed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ),
        sa.ForeignKeyConstraint(['party_id'], ['parties.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_vendor_metrics_organisation_id'), 'vendor_metrics', ['organisation_id'], unique=False)
    op.create_index(op.f('ix_vendor_metrics_party_id'), 'vendor_metrics', ['party_id'], unique=False)
    op.create_index(op.f('ix_vendor_metrics_metric'), 'vendor_metrics', ['metric'], unique=False)
    # Speeds up app/parties/reliability.py's `DISTINCT ON (metric) ... ORDER
    # BY metric, computed_at DESC` current-snapshot query — the one read
    # path every request against this table actually takes.
    op.create_index(
        'ix_vendor_metrics_party_segment_computed_at', 'vendor_metrics',
        ['party_id', 'segment_event_archetype', 'metric', sa.text('computed_at DESC')],
    )

    op.execute("ALTER TABLE vendor_metrics ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE vendor_metrics FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY tenant_isolation ON vendor_metrics
          USING (organisation_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid);
    """)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON vendor_metrics;")
    op.execute("ALTER TABLE vendor_metrics DISABLE ROW LEVEL SECURITY;")

    op.drop_index('ix_vendor_metrics_party_segment_computed_at', table_name='vendor_metrics')
    op.drop_index(op.f('ix_vendor_metrics_metric'), table_name='vendor_metrics')
    op.drop_index(op.f('ix_vendor_metrics_party_id'), table_name='vendor_metrics')
    op.drop_index(op.f('ix_vendor_metrics_organisation_id'), table_name='vendor_metrics')
    op.drop_table('vendor_metrics')

    op.drop_column('projects', 'archetype_code')

    op.drop_constraint('parties_vendor_category_term_id_fkey', 'parties', type_='foreignkey')
    op.drop_index(op.f('ix_parties_vendor_category_term_id'), table_name='parties')
    op.drop_column('parties', 'city')
    op.drop_column('parties', 'vendor_category_term_id')

    op.execute("DROP TYPE IF EXISTS vendor_metric_name;")
