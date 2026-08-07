"""seed deviation_class ontology terms

Revision ID: ab0dc47865c9
Revises: 45309b1d751f
Create Date: 2026-08-08 04:05:00.000000

seed_data/deviation_classes.py's own docstring explains why this seeds
vertical-scoped (event-production) from the start rather than repeating
commitment_act/milestone_type's original universal-core shortcut.
"""
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from seed_data.deviation_classes import DEVIATION_CLASSES
from seed_data.verticals import PLATFORM_VERTICALS

# revision identifiers, used by Alembic.
revision: str = 'ab0dc47865c9'
down_revision: Union[str, Sequence[str], None] = '45309b1d751f'
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
                "category": "deviation_class",
                "code": code,
                "label_en": label_en,
                "label_zh": label_zh,
                "vertical_id": vertical_id,
                "sort_order": i,
                "active": True,
                "effective_from": now,
                "version": 1,
            }
            for i, (code, label_en, label_zh) in enumerate(DEVIATION_CLASSES)
        ],
    )


def downgrade() -> None:
    op.execute("DELETE FROM ontology_terms WHERE category = 'deviation_class';")
