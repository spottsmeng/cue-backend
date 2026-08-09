"""hardening: llm_usage_events, risk_source model_drift value

Revision ID: 0a76bb463d69
Revises: 9197d521030d
Create Date: 2026-08-09 14:00:00.000000

Prompt 13 (M10, PRD §7.7 NFR-OBS-03/05, §8.1 FR-VOI-06):
- `llm_usage_events`: NFR-OBS-03's per-model cost/token accounting,
  attributed to project_id/organisation_id — the "lightweight internal
  table" substitute for standing up Langfuse this session (see
  app/llm/cost.py's own docstring for the named swap-out path). Org-scoped
  RLS, same direct-organisation_id shape and exact policy body as
  foresight_thresholds/retention_policies
  (alembic/versions/45309b1d751f_..._risks_deviations_.py).
- `risk_source` gains 'model_drift': app/observability/drift.py's two
  scheduled jobs (extraction-accuracy drift against cue-eval's baseline,
  ASR WER/CER drift against a held-out labelled set) both raise Risks
  through this new source value, kept distinct from
  app/documents/drift.py's unrelated "drift" (source="contradiction",
  a circulated file differing from its approved DocumentVersion by hash).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0a76bb463d69'
down_revision: Union[str, Sequence[str], None] = '9197d521030d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- risk_source: add 'model_drift'.
    # Postgres 12+ allows ADD VALUE inside a transaction as long as the new
    # value isn't used in the same transaction — same ADD VALUE pattern
    # 9197d521030d's own audit_action extension already established.
    op.execute("ALTER TYPE risk_source ADD VALUE IF NOT EXISTS 'model_drift';")

    # --- llm_usage_events (NFR-OBS-03).
    op.create_table(
        'llm_usage_events',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('organisation_id', postgresql.UUID(as_uuid=True), nullable=False),
        # Nullable: not every call site has a single project in scope (e.g.
        # a future org-wide job) — project_id is an attribution detail, not
        # the tenancy boundary (organisation_id is, per RLS below).
        sa.Column('project_id', postgresql.UUID(as_uuid=True), nullable=True),
        # 'extraction' | 'reasoning' — mirrors app/llm/factory.py's Role.
        sa.Column('role', sa.String(), nullable=False),
        # Free-text call-site identifier (e.g. "ledger_extraction",
        # "ask_intent_classification") — deliberately not an Enum: new
        # call sites shouldn't need a migration to be attributable.
        sa.Column('purpose', sa.String(), nullable=False),
        sa.Column('provider', sa.String(), nullable=False),
        sa.Column('model', sa.String(), nullable=False),
        sa.Column('tokens_in', sa.Integer(), nullable=True),
        sa.Column('tokens_out', sa.Integer(), nullable=True),
        sa.Column('estimated_cost_usd', sa.Numeric(12, 6), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='SET NULL'),
    )
    op.create_index(
        op.f('ix_llm_usage_events_organisation_id'), 'llm_usage_events', ['organisation_id']
    )
    op.create_index(
        op.f('ix_llm_usage_events_project_id'), 'llm_usage_events', ['project_id']
    )
    op.create_index(
        op.f('ix_llm_usage_events_created_at'), 'llm_usage_events', ['created_at']
    )

    op.execute("ALTER TABLE llm_usage_events ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE llm_usage_events FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY tenant_isolation ON llm_usage_events
          USING (organisation_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid);
    """)


def downgrade() -> None:
    op.drop_index(op.f('ix_llm_usage_events_created_at'), table_name='llm_usage_events')
    op.drop_index(op.f('ix_llm_usage_events_project_id'), table_name='llm_usage_events')
    op.drop_index(op.f('ix_llm_usage_events_organisation_id'), table_name='llm_usage_events')
    op.drop_table('llm_usage_events')
    # risk_source: Postgres doesn't support removing an enum value; 'model_drift'
    # stays defined (harmless, matches the codebase's own existing posture on
    # ADD VALUE migrations — none of them write a corresponding DROP VALUE).
