"""seed milestone_type ontology terms

Revision ID: 83b72f2a9175
Revises: 1b4a45ebc039
Create Date: 2026-08-07 16:28:07.380445

"""
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


# revision identifiers, used by Alembic.
revision: str = '83b72f2a9175'
down_revision: Union[str, Sequence[str], None] = '1b4a45ebc039'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# CUE-PRD.md §4.2's 18 milestone types, in the table's own order. Per Prompt
# 4's explicit instruction, seeded universal core (vertical_id AND
# organisation_id both NULL), same shape as b32b62ca45e4's commitment_act
# seed — even though §4.2.1's layering table places "Milestone types" in the
# event-production *vertical pack*, not the universal core. That's a
# deliberate simplification, not an oversight: §4.2.1 itself is scoped
# "internal — not part of the Pico v1 scope" (there is exactly one vertical
# live today), and universal-core rows are what every existing lookup in this
# codebase (_get_commitment_act_term's shape) already knows how to query
# without a vertical_id filter. Re-keying these 18 rows to a real
# event-production vertical_id, the day a second vertical actually ships, is
# a mechanical follow-up migration — codes are stable machine keys
# (ontology_terms.code's own comment: "never renamed once referenced"), so
# nothing that references them by code breaks when that happens.
#
# Five of these — fnb_confirmation, content_approval, contractor_move_in,
# exhibitor_check_in, exhibits_ready — come directly from the Challenge
# Brief's own Annex A (pages 8-9: Project Timeline + Operation Schedule &
# Overtime), not the FR list alone; see CUE-PRD.md §4.2's paragraph right
# after the ontology table for the full citation.
MILESTONE_TYPES = [
    ("design_approval", "Design approval", "设计批准"),
    ("artwork_submission", "Artwork submission", "美术稿提交"),
    ("test_print", "Test print", "打样测试"),
    ("shop_drawing_confirmation", "Shop drawing confirmation", "车间图确认"),
    ("permit_issuance", "Permit issuance", "许可证签发"),
    ("fnb_confirmation", "F&B confirmation", "餐饮确认"),
    ("content_approval", "Content approval", "内容批准"),
    ("contractor_move_in", "Contractor move-in", "承包商进场"),
    ("exhibitor_check_in", "Exhibitor check-in", "参展商签到"),
    ("load_in", "Load-in", "货物进场"),
    ("rigging", "Rigging", "吊装"),
    ("install", "Install", "安装"),
    ("content_load", "Content load", "内容加载"),
    ("exhibits_ready", "Exhibits ready", "展品就绪"),
    ("rehearsal", "Rehearsal", "彩排"),
    ("doors", "Doors", "开幕"),
    ("strike", "Strike", "拆展"),
    ("crate_collection", "Crate collection", "箱柜回收"),
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
                "category": "milestone_type",
                "code": code,
                "label_en": label_en,
                "label_zh": label_zh,
                "sort_order": i,
                "active": True,
                "effective_from": now,
                "version": 1,
            }
            for i, (code, label_en, label_zh) in enumerate(MILESTONE_TYPES)
        ],
    )


def downgrade() -> None:
    op.execute("DELETE FROM ontology_terms WHERE category = 'milestone_type';")
