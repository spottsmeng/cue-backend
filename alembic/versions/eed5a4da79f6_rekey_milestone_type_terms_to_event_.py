"""rekey milestone_type terms to event-production vertical

Revision ID: eed5a4da79f6
Revises: a9199a78e2cf
Create Date: 2026-08-07 17:24:58.481206

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from seed_data.verticals import PLATFORM_VERTICALS

# revision identifiers, used by Alembic.
revision: str = 'eed5a4da79f6'
down_revision: Union[str, Sequence[str], None] = 'a9199a78e2cf'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 83b72f2a9175 seeded these 18 rows as universal-core (vertical_id NULL),
# per Prompt 4's own explicit instruction, back when exactly one vertical
# existed and the multi-vertical layering (CUE-PRD.md §4.2.1) was correctly
# treated as "not part of v1 scope." With a second vertical (renovation/
# interior-design) now a real near-term roadmap item, that shape stops being
# a simplification and starts being a collision risk — a renovation
# `design_approval` is not the event-production `design_approval`, and
# universal-core is exactly the tier where every vertical's terms would
# have to share one namespace. This re-keys them onto the correct tier
# (CUE-PRD.md §4.2.1's own table: "Milestone types" is vertical-pack, not
# universal core) — codes are untouched, so nothing that references them by
# code (app/twin/service.py, seed_data/event_production_archetype.py) breaks.
#
# 83b72f2a9175 itself is not edited to seed vertical-scoped rows directly —
# it's an already-applied historical migration; correcting a shipped
# migration's own seed shape is what a follow-up re-key migration is for,
# not a reason to rewrite history.
EVENT_PRODUCTION_CODE = next(code for code, _name in PLATFORM_VERTICALS if code == "event-production")


def upgrade() -> None:
    op.execute(
        f"""
        UPDATE ontology_terms
        SET vertical_id = (SELECT id FROM verticals WHERE code = '{EVENT_PRODUCTION_CODE}')
        WHERE category = 'milestone_type' AND vertical_id IS NULL;
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        UPDATE ontology_terms
        SET vertical_id = NULL
        WHERE category = 'milestone_type'
          AND vertical_id = (SELECT id FROM verticals WHERE code = '{EVENT_PRODUCTION_CODE}');
        """
    )
