"""seed platform channel_types

Revision ID: d8e1b5f2a634
Revises: c3f6a2b9d417
Create Date: 2026-08-08 16:05:00.000000

seed_data/channel_types.py's own docstring explains the code list.

"""
import uuid
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from seed_data.channel_types import CHANNEL_TYPES

# revision identifiers, used by Alembic.
revision: str = 'd8e1b5f2a634'
down_revision: Union[str, Sequence[str], None] = 'c3f6a2b9d417'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

channel_types = sa.table(
    "channel_types",
    sa.column("id", UUID(as_uuid=True)),
    sa.column("code", sa.String),
    sa.column("capability", sa.String),
    sa.column("active", sa.Boolean),
)


def upgrade() -> None:
    op.bulk_insert(
        channel_types,
        [
            {"id": uuid.uuid4(), "code": code, "capability": capability, "active": True}
            for code, capability in CHANNEL_TYPES
        ],
    )


def downgrade() -> None:
    op.execute("DELETE FROM channel_types WHERE organisation_id IS NULL;")
