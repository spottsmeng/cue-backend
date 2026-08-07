"""twin_audit_log fks set null on delete

Revision ID: e771d0318751
Revises: d7def2e27c7c
Create Date: 2026-08-07 17:32:44.647717

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e771d0318751'
down_revision: Union[str, Sequence[str], None] = 'd7def2e27c7c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 616998fb7f1b's twin_audit_log.milestone_id/dependency_id FKs had no
    # ON DELETE behaviour (default RESTRICT) — harmless while nothing ever
    # deleted a milestone or dependency. app/api/milestones.py's new
    # DELETE endpoints change that: an audit trail entry documenting "this
    # milestone was deleted" must be able to outlive the milestone it
    # references, the same way the rest of this codebase treats audit
    # history as append-only and independent of its subject's lifecycle —
    # SET NULL, not CASCADE, since deleting the milestone must never delete
    # the record that it happened.
    op.drop_constraint('twin_audit_log_milestone_id_fkey', 'twin_audit_log', type_='foreignkey')
    op.create_foreign_key(
        'twin_audit_log_milestone_id_fkey', 'twin_audit_log', 'milestones',
        ['milestone_id'], ['id'], ondelete='SET NULL',
    )
    op.drop_constraint('twin_audit_log_dependency_id_fkey', 'twin_audit_log', type_='foreignkey')
    op.create_foreign_key(
        'twin_audit_log_dependency_id_fkey', 'twin_audit_log', 'dependencies',
        ['dependency_id'], ['id'], ondelete='SET NULL',
    )

    # --- The other half of this same fix, found by actually exercising a
    # DELETE under the append-only trigger: `ON DELETE SET NULL` above is
    # implemented by Postgres as a real `UPDATE twin_audit_log SET
    # milestone_id = NULL ...` when the referenced row is deleted — and
    # 616998fb7f1b's forbid_twin_audit_log_mutation trigger blocked *every*
    # UPDATE unconditionally, including this system-generated one. Without
    # this, deleting any milestone or dependency that had ever been audited
    # would itself fail. Redefined (not a new trigger) with the same one-
    # legitimate-mutation carve-out app/identity/models.py's Delegation
    # already established for revocation: every column stays frozen except
    # milestone_id/dependency_id transitioning to NULL.
    op.execute("""
        CREATE OR REPLACE FUNCTION forbid_twin_audit_log_mutation() RETURNS trigger AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'twin_audit_log is append-only (FR-TWN-10): DELETE not permitted';
          END IF;
          IF NEW.project_id IS DISTINCT FROM OLD.project_id
             OR NEW.action IS DISTINCT FROM OLD.action
             OR NEW.actor_id IS DISTINCT FROM OLD.actor_id
             OR NEW.occurred_at IS DISTINCT FROM OLD.occurred_at
             OR NEW.detail IS DISTINCT FROM OLD.detail
             OR (NEW.milestone_id IS DISTINCT FROM OLD.milestone_id AND NEW.milestone_id IS NOT NULL)
             OR (NEW.dependency_id IS DISTINCT FROM OLD.dependency_id AND NEW.dependency_id IS NOT NULL)
          THEN
            RAISE EXCEPTION 'twin_audit_log is append-only except SET NULL on delete (FR-TWN-10): only milestone_id/dependency_id may be nulled';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)


def downgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION forbid_twin_audit_log_mutation() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'twin_audit_log is append-only (FR-TWN-10): % not permitted', TG_OP;
        END;
        $$ LANGUAGE plpgsql;
    """)

    op.drop_constraint('twin_audit_log_dependency_id_fkey', 'twin_audit_log', type_='foreignkey')
    op.create_foreign_key(
        'twin_audit_log_dependency_id_fkey', 'twin_audit_log', 'dependencies', ['dependency_id'], ['id'],
    )
    op.drop_constraint('twin_audit_log_milestone_id_fkey', 'twin_audit_log', type_='foreignkey')
    op.create_foreign_key(
        'twin_audit_log_milestone_id_fkey', 'twin_audit_log', 'milestones', ['milestone_id'], ['id'],
    )
