"""add user high_contrast preference

Revision ID: 5d68b34f2fa8
Revises: f4b7c2a91e3d
Create Date: 2026-08-11 11:05:11.853215

NFR-ACC-03's per-user high-contrast preference (frontend F9) — see
app/identity/models.py's User.high_contrast for the full reasoning.

Hand-trimmed from the raw `alembic revision --autogenerate` output: that run
also picked up a large amount of pre-existing column-comment drift and a
handful of index adds/drops unrelated to this change (models.py's docstrings
have been edited many times across prior sessions without a matching
comment-only migration each time) — left untouched here, out of scope for
this one-column addition, per this project's own "change one thing at a
time" discipline.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '5d68b34f2fa8'
down_revision: Union[str, Sequence[str], None] = 'f4b7c2a91e3d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column(
            'high_contrast', sa.Boolean(), server_default='false', nullable=False,
            comment=(
                "NFR-ACC-03's high-contrast mode preference — genuinely per-user "
                "(follows a low-vision user across devices), unlike the frontend's "
                "own theme toggle, which is a deliberate device-local localStorage "
                "preference with no backend row (frontend/lib/store/ui-store.ts)."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column('users', 'high_contrast')
