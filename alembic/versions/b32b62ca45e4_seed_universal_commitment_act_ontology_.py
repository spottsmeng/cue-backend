"""seed universal commitment_act ontology terms

Revision ID: b32b62ca45e4
Revises: a895ae03ec5c
Create Date: 2026-08-06 17:18:41.447773

"""
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


# revision identifiers, used by Alembic.
revision: str = 'b32b62ca45e4'
down_revision: Union[str, Sequence[str], None] = 'a895ae03ec5c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Universal core only (CUE-Tech-Stack.md §5.1): the speech-act typology from
# PRD §4.2, not industry vocabulary — vertical_id and organisation_id both
# NULL. The event-production vertical pack (phases, milestone types,
# deliverable classes, vendor categories, venue constructs) and the
# construction-adjacent deviation_class terms are a separate seed, not done
# here — this migration exists only because the extractor needs act_type_id
# lookups to resolve.
COMMITMENT_ACTS = [
    ("offer", "Offer", "提议"),
    ("commit", "Commit", "承诺"),
    ("confirm", "Confirm", "确认"),
    ("revoke", "Revoke", "撤销"),
    ("renegotiate", "Renegotiate", "重新协商"),
    ("quote", "Quote", "报价"),
    ("approve", "Approve", "批准"),
    ("escalate", "Escalate", "升级"),
    ("query", "Query", "询问"),
]

ontology_terms = sa.table(
    "ontology_terms",
    sa.column("id", UUID(as_uuid=True)),
    sa.column("category", sa.String),
    sa.column("code", sa.String),
    sa.column("label_en", sa.String),
    sa.column("label_zh", sa.String),
    sa.column("sort_order", sa.Integer),
    sa.column("active", sa.Boolean),
    sa.column("effective_from", sa.DateTime(timezone=True)),
    sa.column("version", sa.Integer),
)


def upgrade() -> None:
    now = datetime.now(timezone.utc)
    op.bulk_insert(
        ontology_terms,
        [
            {
                "id": uuid.uuid4(),
                "category": "commitment_act",
                "code": code,
                "label_en": label_en,
                "label_zh": label_zh,
                "sort_order": i,
                "active": True,
                "effective_from": now,
                "version": 1,
            }
            for i, (code, label_en, label_zh) in enumerate(COMMITMENT_ACTS)
        ],
    )


def downgrade() -> None:
    op.execute("DELETE FROM ontology_terms WHERE category = 'commitment_act';")
