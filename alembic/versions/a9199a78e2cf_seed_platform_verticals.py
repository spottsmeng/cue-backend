"""seed platform verticals

Revision ID: a9199a78e2cf
Revises: 9b2f8bc21d89
Create Date: 2026-08-07 17:24:58.315368

"""
import uuid
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from seed_data.verticals import PLATFORM_VERTICALS

# revision identifiers, used by Alembic.
revision: str = 'a9199a78e2cf'
down_revision: Union[str, Sequence[str], None] = '9b2f8bc21d89'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# A gap from an earlier session, found while wiring the Twin's own vertical
# scoping: `verticals` (a895ae03ec5c) has never had a seed migration at all.
# ProjectCreate.vertical_code has defaulted to 'event-production' since the
# identity/RBAC session, and app/api/projects.py's create_project 422s on an
# unknown vertical_code — meaning project creation has been silently broken
# in any environment that didn't have someone manually INSERT this row by
# hand outside of migrations. tests/conftest.py's `seeded_vertical_id`
# fixture masked this for the test suite by creating it itself.
ontology_verticals = sa.table(
    "verticals",
    sa.column("id", UUID(as_uuid=True)),
    sa.column("code", sa.String),
    sa.column("name", sa.String),
    sa.column("active", sa.Boolean),
)


def upgrade() -> None:
    op.bulk_insert(
        ontology_verticals,
        [
            {"id": uuid.uuid4(), "code": code, "name": name, "active": True}
            for code, name in PLATFORM_VERTICALS
        ],
    )


def downgrade() -> None:
    # Matches every other seed migration's downgrade style in this repo
    # (plain literal SQL, no bind params) — codes are trusted, git-reviewed
    # constants from seed_data/, not external input.
    codes = ", ".join(f"'{code}'" for code, _name in PLATFORM_VERTICALS)
    op.execute(f"DELETE FROM verticals WHERE code IN ({codes});")
