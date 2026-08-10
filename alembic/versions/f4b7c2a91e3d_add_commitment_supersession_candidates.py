"""add commitment_supersession_candidates

Revision ID: f4b7c2a91e3d
Revises: 0a76bb463d69
Create Date: 2026-08-10 22:00:00.000000

FR-LED-05: closes the "revision_churn/price_drift_pct always unavailable"
gap the Vendor Reliability Graph (Prompt 10/F6) surfaced honestly rather
than worked around — Commitment.supersedes has never been populated by any
path in this build. Rather than either (a) an unreviewed automatic link, or
(b) a purely-manual one nobody reliably uses, this is the AI-proposes/
human-confirms shape already proven three times in this codebase
(Deviation's auto_drafted->confirmed, OutboundMessage's draft->authorised->
sent, Commitment's own pending_verification->human_verified): a candidate
row app/ledger/supersession.py's propose_supersession_candidates writes when
the model judges two commitments look like a revision pair, which a human
then confirms or rejects — only a confirmed row ever mutates Commitment.
supersedes.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'f4b7c2a91e3d'
down_revision: Union[str, Sequence[str], None] = '0a76bb463d69'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "CREATE TYPE commitment_supersession_candidate_status AS ENUM "
        "('pending', 'confirmed', 'rejected');"
    )

    op.create_table(
        'commitment_supersession_candidates',
        sa.Column('project_id', sa.UUID(), nullable=False, comment='RLS scope'),
        sa.Column('commitment_id', sa.UUID(), nullable=False, comment='the newer commitment — the one that may be a revision'),
        sa.Column('supersedes_commitment_id', sa.UUID(), nullable=False, comment='the older commitment it may revise'),
        sa.Column('reasoning', sa.String(), nullable=False, comment="the model's own stated reasoning, verbatim"),
        sa.Column(
            'status',
            postgresql.ENUM(
                'pending', 'confirmed', 'rejected',
                name='commitment_supersession_candidate_status', create_type=False,
            ),
            nullable=False,
        ),
        sa.Column('reviewed_by', sa.UUID(), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['commitment_id'], ['commitments.id'], ),
        sa.ForeignKeyConstraint(['supersedes_commitment_id'], ['commitments.id'], ),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
        sa.ForeignKeyConstraint(['reviewed_by'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_commitment_supersession_candidates_project_id'),
        'commitment_supersession_candidates', ['project_id'], unique=False,
    )
    op.create_index(
        op.f('ix_commitment_supersession_candidates_commitment_id'),
        'commitment_supersession_candidates', ['commitment_id'], unique=False,
    )
    op.create_index(
        op.f('ix_commitment_supersession_candidates_supersedes_commitment_id'),
        'commitment_supersession_candidates', ['supersedes_commitment_id'], unique=False,
    )

    # RLS — same direct-project_id-column, join-through-projects shape
    # 45309b1d751f already established for risks/deviations/notifications/
    # webhook_subscriptions/quiet_hours_configs.
    op.execute("ALTER TABLE commitment_supersession_candidates ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE commitment_supersession_candidates FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY tenant_isolation ON commitment_supersession_candidates
          USING (project_id IN (
            SELECT id FROM projects
            WHERE organisation_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
          ));
    """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON commitment_supersession_candidates;")
    op.drop_index(
        op.f('ix_commitment_supersession_candidates_supersedes_commitment_id'),
        table_name='commitment_supersession_candidates',
    )
    op.drop_index(
        op.f('ix_commitment_supersession_candidates_commitment_id'),
        table_name='commitment_supersession_candidates',
    )
    op.drop_index(
        op.f('ix_commitment_supersession_candidates_project_id'),
        table_name='commitment_supersession_candidates',
    )
    op.drop_table('commitment_supersession_candidates')
    op.execute("DROP TYPE commitment_supersession_candidate_status;")
