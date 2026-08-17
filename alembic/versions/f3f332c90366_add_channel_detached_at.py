"""add channel detached_at

Revision ID: f3f332c90366
Revises: a1c3e6f9d2b7
Create Date: 2026-08-17 09:00:00.000000

DELETE .../channels/{id} used to hard-delete the Channel row, which
Postgres correctly rejects once any Message (or Evidence built on one)
still references it (messages_channel_id_fkey has no ON DELETE action —
by design, see CLAUDE.md's "no commitment without evidence"). Detach is
a soft-delete instead: same additive/nullable shape as Project.archived_at
(app/models/project.py). Every existing Channel row is unaffected
(NULL = still attached).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f3f332c90366'
down_revision: Union[str, Sequence[str], None] = 'a1c3e6f9d2b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'channels',
        sa.Column(
            'detached_at', sa.DateTime(timezone=True), nullable=True,
            comment=(
                "Soft-delete marker — see Channel.detached_at's own "
                "docstring (app/models/project.py) for the full reasoning."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column('channels', 'detached_at')
