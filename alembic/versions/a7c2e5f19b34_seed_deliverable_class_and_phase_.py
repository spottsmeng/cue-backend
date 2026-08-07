"""seed deliverable_class and phase ontology terms

Revision ID: a7c2e5f19b34
Revises: f3a1c9d7e4b2
Create Date: 2026-08-07 21:05:00.000000

CUE-PRD.md §4.2's ontology table, the two categories Document.class_term_id
and Document.phase_term_id (app/documents/models.py) need populated to be
usable at all — neither had ever been seeded by any migration before this
one (unlike commitment_act/milestone_type, seeded in b32b62ca45e4 and
83b72f2a9175 respectively). Seeded universal core (vertical_id AND
organisation_id both NULL), same deliberate v1 simplification
83b72f2a9175's own comment documents for milestone_type — §4.2.1 places both
"Deliverable classes" and "Phases" in the event-production vertical pack,
but there is exactly one vertical live today and every existing lookup
pattern in this codebase already queries universal-core rows without a
vertical_id filter. Re-keying to a real vertical_id is a mechanical
follow-up once a second vertical ships (codes are stable, per
ontology_terms.code's own "never renamed once referenced" comment).
"""
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision: str = 'a7c2e5f19b34'
down_revision: Union[str, Sequence[str], None] = 'f3a1c9d7e4b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DELIVERABLE_CLASSES = [
    ("booth_structure", "Booth structure", "展位结构"),
    ("graphic_panel", "Graphic panel", "图形展板"),
    ("av_equipment", "AV equipment", "视听设备"),
    ("led_screen", "LED screen", "LED屏幕"),
    ("content_asset", "Content asset", "内容素材"),
    ("furniture", "Furniture", "家具"),
    ("floral", "Floral", "花艺"),
    ("fnb_service", "F&B service", "餐饮服务"),
    ("staffing_roster", "Staffing roster", "人员排班"),
    ("permit", "Permit", "许可证"),
    ("signage", "Signage", "标识"),
]

PHASES = [
    ("pre_production", "Pre-production", "筹备期"),
    ("design", "Design", "设计"),
    ("fabrication", "Fabrication", "制作"),
    ("logistics", "Logistics", "物流"),
    ("bump_in", "Bump-in (build-up)", "进场搭建"),
    ("rehearsal", "Rehearsal", "彩排"),
    ("live", "Live", "活动进行"),
    ("bump_out", "Bump-out (teardown)", "拆场"),
    ("reconciliation", "Reconciliation", "结算"),
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
                "category": "deliverable_class",
                "code": code,
                "label_en": label_en,
                "label_zh": label_zh,
                "sort_order": i,
                "active": True,
                "effective_from": now,
                "version": 1,
            }
            for i, (code, label_en, label_zh) in enumerate(DELIVERABLE_CLASSES)
        ],
    )
    op.bulk_insert(
        ontology_terms,
        [
            {
                "id": uuid.uuid4(),
                "category": "phase",
                "code": code,
                "label_en": label_en,
                "label_zh": label_zh,
                "sort_order": i,
                "active": True,
                "effective_from": now,
                "version": 1,
            }
            for i, (code, label_en, label_zh) in enumerate(PHASES)
        ],
    )


def downgrade() -> None:
    op.execute("DELETE FROM ontology_terms WHERE category = 'phase';")
    op.execute("DELETE FROM ontology_terms WHERE category = 'deliverable_class';")
