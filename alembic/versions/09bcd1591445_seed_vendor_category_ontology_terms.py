"""seed vendor_category ontology terms

Revision ID: 09bcd1591445
Revises: f0c711e76ab4
Create Date: 2026-08-08 05:05:00.000000

seed_data/vendor_categories.py's own docstring explains why this seeds
vertical-scoped (event-production) from the start, same reasoning
ab0dc47865c9 already applied to deviation_class.
"""
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from seed_data.vendor_categories import VENDOR_CATEGORIES
from seed_data.verticals import PLATFORM_VERTICALS

# revision identifiers, used by Alembic.
revision: str = '09bcd1591445'
down_revision: Union[str, Sequence[str], None] = 'f0c711e76ab4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

EVENT_PRODUCTION_CODE = next(code for code, _name in PLATFORM_VERTICALS if code == "event-production")

ontology_terms = sa.table(
    "ontology_terms",
    sa.column("id", UUID(as_uuid=True)),
    sa.column("category", sa.String),
    sa.column("code", sa.String),
    sa.column("label_en", sa.String),
    sa.column("label_zh", sa.String),
    sa.column("vertical_id", UUID(as_uuid=True)),
    sa.column("sort_order", sa.Integer),
    sa.column("active", sa.Boolean),
    sa.column("effective_from", sa.DateTime(timezone=True)),
    sa.column("version", sa.Integer),
)


def upgrade() -> None:
    connection = op.get_bind()
    vertical_id = connection.execute(
        sa.text("SELECT id FROM verticals WHERE code = :code"), {"code": EVENT_PRODUCTION_CODE}
    ).scalar_one()

    now = datetime.now(timezone.utc)
    op.bulk_insert(
        ontology_terms,
        [
            {
                "id": uuid.uuid4(),
                "category": "vendor_category",
                "code": code,
                "label_en": label_en,
                "label_zh": label_zh,
                "vertical_id": vertical_id,
                "sort_order": i,
                "active": True,
                "effective_from": now,
                "version": 1,
            }
            for i, (code, label_en, label_zh) in enumerate(VENDOR_CATEGORIES)
        ],
    )


def downgrade() -> None:
    op.execute("DELETE FROM ontology_terms WHERE category = 'vendor_category';")
