"""seed default event-production archetype

Revision ID: 9b2f8bc21d89
Revises: 616998fb7f1b
Create Date: 2026-08-07 16:28:07.714370

"""
import uuid
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


# revision identifiers, used by Alembic.
revision: str = '9b2f8bc21d89'
down_revision: Union[str, Sequence[str], None] = '616998fb7f1b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# FR-TWN-09's expert-seeded default, converted directly from the Challenge
# Brief's own Annex A (pages 8-9 — read directly, not just CUE-PRD.md's
# summary of it): the Project Timeline (Design and Layout / Content & Media /
# Misc) and the Operation Schedule & Overtime table. `day_offset` is days
# relative to the project's event_start (day 0 = doors/exhibition opens,
# 24 June 2026 in Annex A's own worked example) — negative before, positive
# after. Sequence order is chronological by day_offset, which is NOT the same
# as CUE-PRD.md §4.2's milestone_type table order (that table is grouped by
# phase-ish category, e.g. "strike" before "crate collection"; Annex A's own
# Tear-down Period rows list crate delivery before booth dismantling, so this
# archetype follows the brief's ordering, not the ontology table's).
#
# Anchored to real Annex A dates:
#   fnb_confirmation           30 Mar  -> -86  (independently earliest, per
#                                                CUE-PRD.md §4.2's own note)
#   design_approval             30 Apr -> -55  (booth design layout confirmation)
#   artwork_submission          14 May -> -41  (graphics artwork/photos + new
#                                                co-exhibitor logo)
#   test_print                  31 May -> -25  (graphics test print approval)
#   content_approval            19 Jun ->  -6  (final video assets/sequence
#                                                approved to load into screen)
#   contractor_move_in          22 Jun ->  -3
#   exhibitor_check_in          23 Jun ->  -2
#   exhibits_ready       23 Jun 22:00 ->  -1  (rounded to the day before doors)
#   doors                        24 Jun ->   0  (exhibition opens — FIXED)
#   strike / crate_collection    26 Jun ->  +2  (teardown period)
#
# Not in Annex A, interpolated to a defensible slot in the same sequence
# (permit issuance, shop drawing confirmation, rigging, install, content
# load, rehearsal, load-in) — flagged here rather than silently blended in
# with the real anchors above, per CLAUDE.md's evidence discipline applied
# to this migration's own reasoning, not just extracted commitments.
ARCHETYPE_ITEMS = [
    # (type_code, name, day_offset, is_fixed)
    ("fnb_confirmation", "F&B confirmation", -86, False),
    ("design_approval", "Booth design layout confirmation", -55, False),
    ("shop_drawing_confirmation", "Shop drawing confirmation", -48, False),  # interpolated
    ("artwork_submission", "Graphics artwork and co-exhibitor logo submission", -41, False),
    ("test_print", "Graphics test print approval", -25, False),
    ("permit_issuance", "Permit issuance", -10, False),  # interpolated
    ("content_approval", "Final video assets and sequence approved", -6, False),
    ("contractor_move_in", "Nominated stand contractors move-in", -3, False),
    ("load_in", "Exhibits move in", -3, False),
    ("rigging", "Rigging", -3, False),  # interpolated, concurrent with move-in window
    ("install", "Booth and graphics install", -2, False),  # interpolated
    ("exhibitor_check_in", "Exhibitor check-in and badge collection", -2, False),
    ("content_load", "Content load into screens", -2, False),  # interpolated
    ("exhibits_ready", "All exhibits ready for display", -1, False),
    ("rehearsal", "Rehearsal", -1, False),  # interpolated
    ("doors", "Exhibition opens", 0, True),
    ("crate_collection", "Forwarder delivers/collects crates", 2, False),
    ("strike", "Booth dismantling", 2, False),
]

ARCHETYPE_CODE = "event-production-default"

milestone_archetypes = sa.table(
    "milestone_archetypes",
    sa.column("id", UUID(as_uuid=True)),
    sa.column("code", sa.String),
    sa.column("vertical_id", UUID(as_uuid=True)),
    sa.column("name", sa.String),
    sa.column("is_default", sa.Boolean),
)

milestone_archetype_items = sa.table(
    "milestone_archetype_items",
    sa.column("id", UUID(as_uuid=True)),
    sa.column("archetype_id", UUID(as_uuid=True)),
    sa.column("sequence_order", sa.Integer),
    sa.column("type_code", sa.String),
    sa.column("name", sa.String),
    sa.column("day_offset", sa.Integer),
    sa.column("is_fixed", sa.Boolean),
)


def upgrade() -> None:
    archetype_id = uuid.uuid4()
    op.bulk_insert(
        milestone_archetypes,
        [
            {
                "id": archetype_id,
                "code": ARCHETYPE_CODE,
                "vertical_id": None,
                "name": "Event production — default (Annex A)",
                "is_default": True,
            }
        ],
    )
    op.bulk_insert(
        milestone_archetype_items,
        [
            {
                "id": uuid.uuid4(),
                "archetype_id": archetype_id,
                "sequence_order": i,
                "type_code": type_code,
                "name": name,
                "day_offset": day_offset,
                "is_fixed": is_fixed,
            }
            for i, (type_code, name, day_offset, is_fixed) in enumerate(ARCHETYPE_ITEMS)
        ],
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM milestone_archetype_items WHERE archetype_id IN "
        f"(SELECT id FROM milestone_archetypes WHERE code = '{ARCHETYPE_CODE}');"
    )
    op.execute(f"DELETE FROM milestone_archetypes WHERE code = '{ARCHETYPE_CODE}';")
