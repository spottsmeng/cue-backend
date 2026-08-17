"""add channel display_name

Revision ID: a1c3e6f9d2b7
Revises: 5d68b34f2fa8
Create Date: 2026-08-17 06:10:00.000000

"Layer B Channel Picker — Implementation Prompt.txt"'s own explicit design
choice (see app/models/project.py's Channel.display_name docstring for the
full reasoning): a resolved WhatsApp conversation name, cached at attach
time rather than re-resolved from Layer A on every read. Nullable/additive
— every existing Channel row (and every non-WhatsApp attach going forward)
simply has none.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a1c3e6f9d2b7'
down_revision: Union[str, Sequence[str], None] = '5d68b34f2fa8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'channels',
        sa.Column(
            'display_name', sa.String(), nullable=True,
            comment=(
                "Human-readable label, cached at attach time — not "
                "authoritative and not kept in sync. See Channel.display_name's "
                "own docstring (app/models/project.py) for the full reasoning."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column('channels', 'display_name')
