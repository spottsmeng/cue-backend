"""add propagation risk source

Wires app/twin/graph.py's propagate() (previously reachable only via the
PM-triggered /twin/propagate what-if endpoint) into the automatic Risk
pipeline every other detector (silence.py, contradiction.py, forecast.py)
already uses — app/foresight/propagation.py is the new detector, raising a
Risk with this source when a milestone reschedule (PATCH .../milestones/{id})
disturbs a downstream dependent.

Same ADD VALUE pattern b2d94f7c1a08 / f3157fe92ddc already established for
enum extension. Not reversible: PostgreSQL has no DROP VALUE, the same
reason those migrations give for their own no-op downgrades.

Revision ID: e69e5ec31a6f
Revises: f3157fe92ddc
Create Date: 2026-08-24 16:02:34.900104

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e69e5ec31a6f'
down_revision: Union[str, Sequence[str], None] = 'f3157fe92ddc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE risk_source ADD VALUE IF NOT EXISTS 'propagation';")


def downgrade() -> None:
    # PostgreSQL cannot remove a value from an enum type; leaving it in place
    # is harmless (no row uses it after the code that writes it is reverted).
    pass
