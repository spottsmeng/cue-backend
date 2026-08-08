"""convert channel_type enum to channel_types FK

Revision ID: f9a4c7e2b850
Revises: d8e1b5f2a634
Create Date: 2026-08-08 16:10:00.000000

channels.type and channel_identities.channel_type become plain VARCHAR
columns FK'd to channel_types.code (natural-key FK, not a surrogate-id
FK — see app/models/channel_type.py's own docstring for why). Pre-launch
codebase (backend/PROGRESS.md: M8 "Not started" as of this migration), so
no production rows to backfill — only test fixtures, and the previous
migration (d8e1b5f2a634) already seeded every code those fixtures use.

evidence.channel is a provenance/display label, not an adapter-selection
key (15+ test fixtures set it to arbitrary channel-type strings unrelated
to channel-adapter behaviour) — it drops the enum type too but stays a
plain string, no FK, so none of those fixtures need to change.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f9a4c7e2b850'
down_revision: Union[str, Sequence[str], None] = 'd8e1b5f2a634'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        'channels', 'type', type_=sa.String(), postgresql_using='type::text'
    )
    op.create_foreign_key(
        'channels_type_channel_types_fkey', 'channels', 'channel_types', ['type'], ['code'],
    )

    op.alter_column(
        'channel_identities', 'channel_type', type_=sa.String(), postgresql_using='channel_type::text'
    )
    op.create_foreign_key(
        'channel_identities_channel_type_channel_types_fkey',
        'channel_identities', 'channel_types', ['channel_type'], ['code'],
    )

    op.alter_column(
        'evidence', 'channel', type_=sa.String(), postgresql_using='channel::text'
    )

    op.execute("DROP TYPE IF EXISTS channel_type;")


def downgrade() -> None:
    op.execute(
        "CREATE TYPE channel_type AS ENUM "
        "('whatsapp', 'wechat', 'teams', 'outlook', 'sharepoint', 'manual');"
    )
    op.execute(
        "ALTER TABLE evidence ALTER COLUMN channel TYPE channel_type USING channel::channel_type;"
    )
    op.drop_constraint(
        'channel_identities_channel_type_channel_types_fkey', 'channel_identities', type_='foreignkey'
    )
    op.execute(
        "ALTER TABLE channel_identities ALTER COLUMN channel_type TYPE channel_type "
        "USING channel_type::channel_type;"
    )
    op.drop_constraint('channels_type_channel_types_fkey', 'channels', type_='foreignkey')
    op.execute(
        "ALTER TABLE channels ALTER COLUMN type TYPE channel_type USING type::channel_type;"
    )
    # Dev-only best-effort rebuild — same accepted posture other irreversible
    # enum-conversion migrations in this repo already document (e.g.
    # d7def2e27c7c's downgrade() for twin_audit_action).
