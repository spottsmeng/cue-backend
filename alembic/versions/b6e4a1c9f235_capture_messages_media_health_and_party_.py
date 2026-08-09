"""capture: messages, message_media, channel_health_events, party_organisation_mappings

Revision ID: b6e4a1c9f235
Revises: f9a4c7e2b850
Create Date: 2026-08-08 20:00:00.000000

Prompt 11 item 1 (PRD FR-NRM-01) plus the schema this milestone's later items
(3, 4, 5, 6, 9) build on: Message (the canonical envelope every
ChannelAdapter's RawCapturedMessage becomes once queued and normalised),
MessageMedia (FR-CAP-12's original-vs-derived-artefact separation),
ChannelHealthEvent (FR-CAP-09's durable health history behind Channel.healthy),
and PartyOrganisationMapping (FR-NRM-04's effective-dated person->vendor
mapping). Also gives Evidence.message_id a real FK — it has read "Points at
the future Message/capture table — not modelled yet" since the very first
migration (a895ae03ec5c); this is what finally resolves that.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b6e4a1c9f235'
down_revision: Union[str, Sequence[str], None] = 'f9a4c7e2b850'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- messages
    op.create_table(
        'messages',
        sa.Column('project_id', sa.UUID(), nullable=False, comment='RLS scope'),
        sa.Column('channel_id', sa.UUID(), nullable=False),
        sa.Column('external_id', sa.String(), nullable=False, comment="the channel's own message id"),
        sa.Column(
            'sender_external_id', sa.String(), nullable=False,
            comment='raw identity string as the channel reports it',
        ),
        sa.Column('author_party_id', sa.UUID(), nullable=True),
        sa.Column('identity_confidence', sa.Float(), nullable=True),
        sa.Column('identity_manually_verified', sa.Boolean(), nullable=False),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('language', sa.String(), nullable=True, comment='FR-NRM-02'),
        sa.Column('text', sa.String(), nullable=True),
        sa.Column('payload_hash', sa.String(), nullable=False, comment='FR-CAP-11 dedup key'),
        sa.Column('extraction_attempted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['author_party_id'], ['parties.id'], ),
        sa.ForeignKeyConstraint(['channel_id'], ['channels.id'], ),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('project_id', 'payload_hash', name='messages_project_payload_hash_key'),
    )
    op.create_index(op.f('ix_messages_project_id'), 'messages', ['project_id'], unique=False)
    op.create_index(op.f('ix_messages_channel_id'), 'messages', ['channel_id'], unique=False)
    op.create_index(op.f('ix_messages_author_party_id'), 'messages', ['author_party_id'], unique=False)

    op.execute("ALTER TABLE messages ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE messages FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY tenant_isolation ON messages
          USING (project_id IN (
            SELECT id FROM projects
            WHERE organisation_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
          ));
    """)

    # --- message_media
    op.create_table(
        'message_media',
        sa.Column('message_id', sa.UUID(), nullable=False),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('original_filename', sa.String(), nullable=True),
        sa.Column('source_uri', sa.String(), nullable=True, comment="adapter's own fetch reference"),
        sa.Column('storage_key', sa.String(), nullable=True),
        sa.Column('thumbnail_key', sa.String(), nullable=True),
        sa.Column('ocr_text', sa.String(), nullable=True),
        sa.Column('exif', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('transcript', sa.String(), nullable=True),
        sa.Column('transcript_confidence', sa.Float(), nullable=True),
        sa.Column('transcript_detail', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('transcript_language', sa.String(), nullable=True),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['message_id'], ['messages.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_message_media_message_id'), 'message_media', ['message_id'], unique=False)

    op.execute("ALTER TABLE message_media ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE message_media FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY tenant_isolation ON message_media
          USING (message_id IN (
            SELECT id FROM messages WHERE project_id IN (
              SELECT id FROM projects
              WHERE organisation_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
            )
          ));
    """)

    # --- channel_health_events
    op.create_table(
        'channel_health_events',
        sa.Column('project_id', sa.UUID(), nullable=False, comment='RLS scope'),
        sa.Column('channel_id', sa.UUID(), nullable=False),
        sa.Column('healthy', sa.Boolean(), nullable=False),
        sa.Column('detail', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('checked_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(['channel_id'], ['channels.id'], ),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_channel_health_events_project_id'), 'channel_health_events', ['project_id'], unique=False
    )
    op.create_index(
        op.f('ix_channel_health_events_channel_id'), 'channel_health_events', ['channel_id'], unique=False
    )

    op.execute("ALTER TABLE channel_health_events ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE channel_health_events FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY tenant_isolation ON channel_health_events
          USING (project_id IN (
            SELECT id FROM projects
            WHERE organisation_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
          ));
    """)

    # --- party_organisation_mappings
    op.create_table(
        'party_organisation_mappings',
        sa.Column('organisation_id', sa.UUID(), nullable=False, comment='RLS scope'),
        sa.Column('person_party_id', sa.UUID(), nullable=False),
        sa.Column('organisation_party_id', sa.UUID(), nullable=False),
        sa.Column('role_title', sa.String(), nullable=True),
        sa.Column('effective_from', sa.DateTime(timezone=True), nullable=False),
        sa.Column('effective_to', sa.DateTime(timezone=True), nullable=True, comment='NULL = still current'),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ),
        sa.ForeignKeyConstraint(['organisation_party_id'], ['parties.id'], ),
        sa.ForeignKeyConstraint(['person_party_id'], ['parties.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_party_organisation_mappings_organisation_id'),
        'party_organisation_mappings', ['organisation_id'], unique=False,
    )
    op.create_index(
        op.f('ix_party_organisation_mappings_person_party_id'),
        'party_organisation_mappings', ['person_party_id'], unique=False,
    )
    op.create_index(
        op.f('ix_party_organisation_mappings_organisation_party_id'),
        'party_organisation_mappings', ['organisation_party_id'], unique=False,
    )

    op.execute("ALTER TABLE party_organisation_mappings ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE party_organisation_mappings FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY tenant_isolation ON party_organisation_mappings
          USING (organisation_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid);
    """)

    # --- Evidence.message_id: real FK at last (app/models/ledger.py's
    # Evidence.message_id docstring / this migration's own module docstring).
    op.create_index(op.f('ix_evidence_message_id'), 'evidence', ['message_id'], unique=False)
    op.create_foreign_key(
        'evidence_message_id_fkey', 'evidence', 'messages', ['message_id'], ['id'],
    )


def downgrade() -> None:
    op.drop_constraint('evidence_message_id_fkey', 'evidence', type_='foreignkey')
    op.drop_index(op.f('ix_evidence_message_id'), table_name='evidence')

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON party_organisation_mappings;")
    op.execute("ALTER TABLE party_organisation_mappings DISABLE ROW LEVEL SECURITY;")
    op.drop_index(
        op.f('ix_party_organisation_mappings_organisation_party_id'),
        table_name='party_organisation_mappings',
    )
    op.drop_index(
        op.f('ix_party_organisation_mappings_person_party_id'), table_name='party_organisation_mappings'
    )
    op.drop_index(
        op.f('ix_party_organisation_mappings_organisation_id'), table_name='party_organisation_mappings'
    )
    op.drop_table('party_organisation_mappings')

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON channel_health_events;")
    op.execute("ALTER TABLE channel_health_events DISABLE ROW LEVEL SECURITY;")
    op.drop_index(op.f('ix_channel_health_events_channel_id'), table_name='channel_health_events')
    op.drop_index(op.f('ix_channel_health_events_project_id'), table_name='channel_health_events')
    op.drop_table('channel_health_events')

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON message_media;")
    op.execute("ALTER TABLE message_media DISABLE ROW LEVEL SECURITY;")
    op.drop_index(op.f('ix_message_media_message_id'), table_name='message_media')
    op.drop_table('message_media')

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON messages;")
    op.execute("ALTER TABLE messages DISABLE ROW LEVEL SECURITY;")
    op.drop_index(op.f('ix_messages_author_party_id'), table_name='messages')
    op.drop_index(op.f('ix_messages_channel_id'), table_name='messages')
    op.drop_index(op.f('ix_messages_project_id'), table_name='messages')
    op.drop_table('messages')
