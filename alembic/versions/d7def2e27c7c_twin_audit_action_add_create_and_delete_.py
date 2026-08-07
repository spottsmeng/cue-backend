"""twin_audit_action add create and delete values

Revision ID: d7def2e27c7c
Revises: fbea5e75b4d8
Create Date: 2026-08-07 17:24:58.811237

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd7def2e27c7c'
down_revision: Union[str, Sequence[str], None] = 'fbea5e75b4d8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # FR-TWN-10's "any dependency" override now includes adding/removing a
    # milestone or edge outright (app/api/milestones.py's POST/DELETE), not
    # just editing an existing one — twin_audit_log needs an action value
    # for each. One ADD VALUE per statement (same constraint d05dce0f415d's
    # 'manual' addition to channel_type already documents) — each new value
    # is usable after this transaction commits, which is fine, nothing in
    # this migration itself uses one.
    op.execute("ALTER TYPE twin_audit_action ADD VALUE IF NOT EXISTS 'milestone_created';")
    op.execute("ALTER TYPE twin_audit_action ADD VALUE IF NOT EXISTS 'milestone_deleted';")
    op.execute("ALTER TYPE twin_audit_action ADD VALUE IF NOT EXISTS 'dependency_created';")
    op.execute("ALTER TYPE twin_audit_action ADD VALUE IF NOT EXISTS 'dependency_deleted';")


def downgrade() -> None:
    # Postgres has no DROP VALUE for enum types — same accepted, documented
    # limitation d05dce0f415d's downgrade() notes for channel_type's
    # 'manual' value. Dev-only downgrade path; not worth a type rebuild.
    pass
