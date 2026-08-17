"""add spec_claim confidence

Revision ID: 06ed0141ee07
Revises: f3f332c90366
Create Date: 2026-08-17 17:00:00.000000

Blind Spots item 4: app/documents/schema.py's ExtractedSpecClaim has
required a model-returned `confidence` per claim since the Documents
milestone (Prompt 6) built it — app/documents/extractor.py's build_prompt
even instructs the model to supply it — but SpecClaim never had a column to
persist it into. Not a §4.3 domain field (SpecClaim's own class docstring's
"do not add or rename fields" is about that schema — Location/Description/
Dimension/Finishing/Qty/Status), the same extraction-metadata carve-out
Commitment.confidence already relies on. Additive/nullable: every existing
SpecClaim row predates this column and has nothing to backfill it from.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '06ed0141ee07'
down_revision: Union[str, Sequence[str], None] = 'f3f332c90366'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'spec_claims',
        sa.Column(
            'confidence', sa.Float(), nullable=True,
            comment="the extraction model's own 0-1 confidence in this claim",
        ),
    )


def downgrade() -> None:
    op.drop_column('spec_claims', 'confidence')
