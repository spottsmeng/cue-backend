"""layer_a observability: health snapshots, conflicts, alerts, config

Revision ID: 09ab3c2fc622
Revises: 06ed0141ee07
Create Date: 2026-08-19 14:45:20.741485

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '09ab3c2fc622'
down_revision: Union[str, Sequence[str], None] = '06ed0141ee07'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # --- Layer A observability: task-layer-A-observability-dashboard-prompt.txt.
    # organisation_id is a direct column on every table here (org-wide ops
    # infra, not project-scoped — same tenant_isolation shape retention_policies
    # already uses, per 9ddb100d7e8e's own comment on that choice), not the
    # project-join shape consent_records/budgets/channels need.

    op.create_table(
        'layer_a_conflict_events',
        sa.Column('organisation_id', sa.UUID(), nullable=False, comment='RLS scope'),
        sa.Column('detected_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('refused_pid', sa.Integer(), nullable=False),
        sa.Column('owner_pid', sa.Integer(), nullable=False),
        sa.Column('ingested_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('organisation_id', 'detected_at', 'refused_pid', name='layer_a_conflict_events_dedup_key'),
    )
    op.create_index(op.f('ix_layer_a_conflict_events_organisation_id'), 'layer_a_conflict_events', ['organisation_id'], unique=False)
    op.execute("ALTER TABLE layer_a_conflict_events ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE layer_a_conflict_events FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY tenant_isolation ON layer_a_conflict_events
          USING (organisation_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid);
    """)

    op.create_table(
        'layer_a_health_snapshots',
        sa.Column('organisation_id', sa.UUID(), nullable=False, comment='RLS scope'),
        sa.Column('account_id', sa.String(), nullable=False, comment="Layer A's own opaque accountId, not a CUE FK"),
        sa.Column('source', sa.Enum('poll', 'transition', name='layer_a_snapshot_source'), nullable=False),
        sa.Column('recorded_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('status', sa.Enum('connecting', 'connected', 'reconnecting', 'unhealthy', 'disconnected', 'unknown', name='layer_a_worker_status'), nullable=False),
        sa.Column('connect_attempts', sa.Integer(), nullable=False),
        sa.Column('last_connected_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_message_timestamp', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.String(), nullable=True),
        sa.Column('last_disconnect_reason', sa.String(), nullable=True, comment="poll rows only — not present on Layer A's /health-history endpoint"),
        sa.Column('last_disconnect_status_code', sa.Integer(), nullable=True, comment='poll rows only — the Boom status code, e.g. 440 = session conflict'),
        sa.Column('healthy', sa.Boolean(), nullable=True, comment='poll rows only'),
        sa.Column('risk_tier', sa.String(), nullable=True, comment='poll rows only'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment="ingestion time, distinct from recorded_at's event time"),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_layer_a_health_snapshots_account_id'), 'layer_a_health_snapshots', ['account_id'], unique=False)
    # Partial — poll rows only. See the model's own __table_args__ comment
    # (app/layer_a/models.py): a plain (non-partial) unique constraint on
    # this key silently dropped a real, distinct transition-row event that
    # happened to share a millisecond timestamp with another one, found via
    # this feature's own real-fixture-server test.
    op.create_index(
        'ix_layer_a_health_snapshots_poll_dedup_key', 'layer_a_health_snapshots',
        ['organisation_id', 'account_id', 'source', 'recorded_at'],
        unique=True, postgresql_where=sa.text("source = 'poll'"),
    )
    op.create_index('ix_layer_a_health_snapshots_account_recorded', 'layer_a_health_snapshots', ['organisation_id', 'account_id', 'recorded_at'], unique=False)
    op.create_index(op.f('ix_layer_a_health_snapshots_organisation_id'), 'layer_a_health_snapshots', ['organisation_id'], unique=False)
    op.execute("ALTER TABLE layer_a_health_snapshots ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE layer_a_health_snapshots FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY tenant_isolation ON layer_a_health_snapshots
          USING (organisation_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid);
    """)

    op.create_table(
        'layer_a_alert_config',
        sa.Column('organisation_id', sa.UUID(), nullable=False, comment='RLS scope'),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('sustained_disconnect_minutes', sa.Integer(), nullable=False),
        sa.Column('reconnect_attempt_threshold', sa.Integer(), nullable=False),
        sa.Column('reconnect_attempt_window_minutes', sa.Integer(), nullable=False),
        sa.Column('webhook_url', sa.String(), nullable=True),
        sa.Column('webhook_secret', sa.String(), nullable=True, comment='HMAC-SHA256 signing key, generated server-side the first time webhook_url is set'),
        sa.Column('webhook_enabled', sa.Boolean(), nullable=False),
        sa.Column('email_recipients', sa.ARRAY(sa.String()), nullable=False),
        sa.Column('email_enabled', sa.Boolean(), nullable=False),
        sa.Column('updated_by', sa.UUID(), nullable=True),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint('reconnect_attempt_threshold > 0', name='layer_a_alert_config_positive_attempt_threshold'),
        sa.CheckConstraint('reconnect_attempt_window_minutes > 0', name='layer_a_alert_config_positive_window_minutes'),
        sa.CheckConstraint('sustained_disconnect_minutes > 0', name='layer_a_alert_config_positive_disconnect_minutes'),
        sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ),
        sa.ForeignKeyConstraint(['updated_by'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('organisation_id', name='layer_a_alert_config_one_per_org'),
    )
    op.create_index(op.f('ix_layer_a_alert_config_organisation_id'), 'layer_a_alert_config', ['organisation_id'], unique=False)
    op.execute("ALTER TABLE layer_a_alert_config ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE layer_a_alert_config FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY tenant_isolation ON layer_a_alert_config
          USING (organisation_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid);
    """)

    op.create_table(
        'layer_a_alerts',
        sa.Column('organisation_id', sa.UUID(), nullable=False, comment='RLS scope'),
        sa.Column('alert_type', sa.Enum('sustained_disconnect', 'reconnect_flapping', 'session_conflict', name='layer_a_alert_type'), nullable=False),
        sa.Column('account_id', sa.String(), nullable=True),
        sa.Column('severity', sa.Enum('serious', 'critical', name='layer_a_alert_severity'), nullable=False),
        sa.Column('state', sa.Enum('open', 'resolved', name='layer_a_alert_state'), nullable=False),
        sa.Column('opened_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('condition_detail', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('acknowledged_by', sa.UUID(), nullable=True),
        sa.Column('acknowledged_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(['acknowledged_by'], ['users.id'], ),
        sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    # Only *open* rows are constrained unique — this is what lets the alert
    # evaluator run every sweep safely via INSERT ... ON CONFLICT DO NOTHING,
    # with no separate "is one already open" query race, while still letting
    # the same (org, alert_type, account) recur as a brand-new alert once a
    # prior one resolves. nulls_not_distinct: a session_conflict alert
    # sourced from a pid-lock refusal has account_id=NULL (process-wide) —
    # without this, Postgres's default "every NULL is distinct" semantics
    # would let two such alerts stay open at once for the same org.
    op.create_index(
        'ix_layer_a_alerts_open_unique', 'layer_a_alerts', ['organisation_id', 'alert_type', 'account_id'],
        unique=True, postgresql_where=sa.text("state = 'open'"), postgresql_nulls_not_distinct=True,
    )
    op.create_index(op.f('ix_layer_a_alerts_organisation_id'), 'layer_a_alerts', ['organisation_id'], unique=False)
    op.execute("ALTER TABLE layer_a_alerts ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE layer_a_alerts FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY tenant_isolation ON layer_a_alerts
          USING (organisation_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid);
    """)

    op.create_table(
        'layer_a_alert_deliveries',
        sa.Column('alert_id', sa.UUID(), nullable=False),
        sa.Column('organisation_id', sa.UUID(), nullable=False, comment='RLS scope, denormalized from the parent alert for direct-column RLS'),
        sa.Column('channel', sa.Enum('webhook', 'email', 'banner', name='layer_a_delivery_channel'), nullable=False),
        sa.Column('attempted_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('success', sa.Boolean(), nullable=False),
        sa.Column('detail', sa.String(), nullable=True, comment='error message on failure'),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(['alert_id'], ['layer_a_alerts.id'], ),
        sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_layer_a_alert_deliveries_alert_id'), 'layer_a_alert_deliveries', ['alert_id'], unique=False)
    op.create_index(op.f('ix_layer_a_alert_deliveries_organisation_id'), 'layer_a_alert_deliveries', ['organisation_id'], unique=False)
    op.execute("ALTER TABLE layer_a_alert_deliveries ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE layer_a_alert_deliveries FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY tenant_isolation ON layer_a_alert_deliveries
          USING (organisation_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid);
    """)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON layer_a_alert_deliveries;")
    op.execute("ALTER TABLE layer_a_alert_deliveries DISABLE ROW LEVEL SECURITY;")
    op.drop_index(op.f('ix_layer_a_alert_deliveries_organisation_id'), table_name='layer_a_alert_deliveries')
    op.drop_index(op.f('ix_layer_a_alert_deliveries_alert_id'), table_name='layer_a_alert_deliveries')
    op.drop_table('layer_a_alert_deliveries')

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON layer_a_alerts;")
    op.execute("ALTER TABLE layer_a_alerts DISABLE ROW LEVEL SECURITY;")
    op.drop_index(op.f('ix_layer_a_alerts_organisation_id'), table_name='layer_a_alerts')
    op.drop_index('ix_layer_a_alerts_open_unique', table_name='layer_a_alerts', postgresql_where=sa.text("state = 'open'"))
    op.drop_table('layer_a_alerts')
    op.execute("DROP TYPE IF EXISTS layer_a_alert_type;")
    op.execute("DROP TYPE IF EXISTS layer_a_alert_severity;")
    op.execute("DROP TYPE IF EXISTS layer_a_alert_state;")

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON layer_a_alert_config;")
    op.execute("ALTER TABLE layer_a_alert_config DISABLE ROW LEVEL SECURITY;")
    op.drop_index(op.f('ix_layer_a_alert_config_organisation_id'), table_name='layer_a_alert_config')
    op.drop_table('layer_a_alert_config')

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON layer_a_health_snapshots;")
    op.execute("ALTER TABLE layer_a_health_snapshots DISABLE ROW LEVEL SECURITY;")
    op.drop_index(op.f('ix_layer_a_health_snapshots_organisation_id'), table_name='layer_a_health_snapshots')
    op.drop_index('ix_layer_a_health_snapshots_account_recorded', table_name='layer_a_health_snapshots')
    op.drop_index(op.f('ix_layer_a_health_snapshots_account_id'), table_name='layer_a_health_snapshots')
    op.drop_table('layer_a_health_snapshots')
    op.execute("DROP TYPE IF EXISTS layer_a_snapshot_source;")
    op.execute("DROP TYPE IF EXISTS layer_a_worker_status;")

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON layer_a_conflict_events;")
    op.execute("ALTER TABLE layer_a_conflict_events DISABLE ROW LEVEL SECURITY;")
    op.drop_index(op.f('ix_layer_a_conflict_events_organisation_id'), table_name='layer_a_conflict_events')
    op.drop_table('layer_a_conflict_events')

    op.execute("DROP TYPE IF EXISTS layer_a_delivery_channel;")
