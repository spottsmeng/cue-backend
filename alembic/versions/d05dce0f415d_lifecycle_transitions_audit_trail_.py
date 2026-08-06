"""lifecycle transitions, audit trail, manual evidence channel

Revision ID: d05dce0f415d
Revises: b32b62ca45e4
Create Date: 2026-08-07 04:09:59.688550

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'd05dce0f415d'
down_revision: Union[str, Sequence[str], None] = 'b32b62ca45e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Reuse of the existing commitment_state type (created by a895ae03ec5c) for
# audit_log.from_state/to_state — create_type=False so this doesn't try to
# CREATE TYPE a second time. audit_action, below, is new and gets created
# implicitly the first time it's used in op.create_table, same as every
# other enum in the initial migration.
commitment_state_type = postgresql.ENUM(
    'proposed', 'committed', 'at_risk', 'delivered', 'broken', 'renegotiated', 'withdrawn',
    name='commitment_state', create_type=False,
)


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'audit_log',
        sa.Column('project_id', sa.UUID(), nullable=False, comment='RLS scope'),
        sa.Column('commitment_id', sa.UUID(), nullable=False),
        sa.Column(
            'action',
            sa.Enum('created', 'verified', 'corrected', 'state_transition', name='audit_action'),
            nullable=False,
        ),
        sa.Column(
            'actor_id', sa.UUID(), nullable=True,
            comment='FK to users table, deferred to identity module',
        ),
        sa.Column(
            'occurred_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column('from_state', commitment_state_type, nullable=True),
        sa.Column('to_state', commitment_state_type, nullable=True),
        sa.Column(
            'evidence_id', sa.UUID(), nullable=True,
            comment='from what evidence, when the mutation has one',
        ),
        sa.Column('detail', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(['commitment_id'], ['commitments.id'], ),
        sa.ForeignKeyConstraint(['evidence_id'], ['evidence.id'], ),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_audit_log_commitment_id'), 'audit_log', ['commitment_id'], unique=False)
    op.create_index(op.f('ix_audit_log_project_id'), 'audit_log', ['project_id'], unique=False)

    # --- Row-level tenant isolation, same pattern as commitments (a895ae03ec5c):
    # audit_log has no organisation_id column of its own, so the policy joins
    # through projects, same fail-closed-on-unset behaviour.
    op.execute("ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE audit_log FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY tenant_isolation ON audit_log
          USING (project_id IN (
            SELECT id FROM projects
            WHERE organisation_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
          ));
    """)

    # --- FR-LED-12: "immutable audit trail" enforced in the DB, not merely by
    # the application never issuing UPDATE/DELETE against this table — the
    # same posture as the evidence-linkage trigger (CLAUDE.md: "verified in
    # code, not trusted").
    op.execute("""
        CREATE OR REPLACE FUNCTION forbid_audit_log_mutation() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'audit_log is append-only (FR-LED-12): % not permitted', TG_OP;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER audit_log_append_only
        BEFORE UPDATE OR DELETE ON audit_log
        FOR EACH ROW EXECUTE FUNCTION forbid_audit_log_mutation();
    """)

    # --- FR-LED-10: a manually created commitment "references the user
    # action as its evidence" — that evidence row needs a channel value, and
    # none of the existing integration-adapter channels (whatsapp/wechat/
    # teams/outlook/sharepoint) describe a human typing into the CUE API
    # directly. ADD VALUE runs fine inside this migration's transaction
    # (Postgres 12+); the new value just can't be *used* until this
    # transaction commits, which is fine — nothing in this migration uses it.
    op.execute("ALTER TYPE channel_type ADD VALUE IF NOT EXISTS 'manual';")

    # --- FR-LCY-01: reject invalid commitment state transitions, checked
    # again in the database (defense in depth — see app/ledger/lifecycle.py
    # for the primary, application-layer copy of this same table and the
    # reasoning for keeping both). Immediate, not deferred: unlike the
    # evidence trigger, a transition's validity is fully determined by
    # OLD/NEW on this one row, nothing to wait for.
    op.execute("""
        CREATE OR REPLACE FUNCTION check_commitment_transition_valid() RETURNS trigger AS $$
        BEGIN
          IF NOT (
            (OLD.state = 'proposed' AND NEW.state IN ('committed', 'withdrawn', 'proposed')) OR
            (OLD.state = 'committed' AND NEW.state IN ('at_risk', 'delivered', 'renegotiated', 'withdrawn')) OR
            (OLD.state = 'at_risk' AND NEW.state IN ('delivered', 'renegotiated', 'broken')) OR
            (OLD.state = 'renegotiated' AND NEW.state IN ('committed', 'withdrawn')) OR
            (OLD.state = 'broken' AND NEW.state IN ('renegotiated'))
          ) THEN
            RAISE EXCEPTION 'invalid commitment state transition: % -> % (FR-LCY-01)', OLD.state, NEW.state;
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER commitment_transition_valid
        BEFORE UPDATE ON commitments
        FOR EACH ROW
        WHEN (NEW.state IS DISTINCT FROM OLD.state)
        EXECUTE FUNCTION check_commitment_transition_valid();
    """)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP TRIGGER IF EXISTS commitment_transition_valid ON commitments;")
    op.execute("DROP FUNCTION IF EXISTS check_commitment_transition_valid();")

    # Postgres has no DROP VALUE for enum types; 'manual' stays in
    # channel_type on downgrade. Accepted, documented limitation — the only
    # alternative (rebuild the type from scratch) risks data loss for any row
    # already using it and isn't worth automating for a dev-only downgrade path.

    op.execute("DROP TRIGGER IF EXISTS audit_log_append_only ON audit_log;")
    op.execute("DROP FUNCTION IF EXISTS forbid_audit_log_mutation();")

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON audit_log;")
    op.execute("ALTER TABLE audit_log DISABLE ROW LEVEL SECURITY;")

    op.drop_index(op.f('ix_audit_log_project_id'), table_name='audit_log')
    op.drop_index(op.f('ix_audit_log_commitment_id'), table_name='audit_log')
    op.drop_table('audit_log')
    op.execute("DROP TYPE IF EXISTS audit_action;")
