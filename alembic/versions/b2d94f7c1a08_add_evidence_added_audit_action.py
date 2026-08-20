"""add evidence_added audit action

Extraction can now conclude that a message is *about* a commitment already on
the ledger rather than being a new promise (cue-eval/schema.json's
`relates_to`, app/ledger/extractor.py's `_attach_evidence_to_existing`). That
attaches an Evidence row to the existing commitment, which is a ledger
mutation, and FR-LED-12 requires every ledger mutation to be auditable — but
none of the existing audit_action values describes it: it is not a "created"
(no commitment was created), not a "state_transition" (no state changed), and
not a "corrected" (no human corrected anything).

Same ADD VALUE pattern d7def2e27c7c / 9ddb100d7e8e / 9197d521030d already
established for this enum. Not reversible: PostgreSQL has no DROP VALUE, the
same reason those migrations give for their own no-op downgrades.

Revision ID: b2d94f7c1a08
Revises: 09ab3c2fc622
"""

from typing import Sequence, Union

from alembic import op

revision: str = 'b2d94f7c1a08'
down_revision: Union[str, Sequence[str], None] = '09ab3c2fc622'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'evidence_added';")


def downgrade() -> None:
    # PostgreSQL cannot remove a value from an enum type; leaving it in place
    # is harmless (no row uses it after the code that writes it is reverted).
    pass
