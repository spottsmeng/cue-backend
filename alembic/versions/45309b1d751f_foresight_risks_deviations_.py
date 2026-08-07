"""foresight: risks, deviations, notifications, webhooks, thresholds, quiet hours

Revision ID: 45309b1d751f
Revises: a7c2e5f19b34
Create Date: 2026-08-08 04:00:00.000000

Prompt 7 (Foresight, PRD §6.9 FR-FOR / §6.7 FR-DEV / §6.15 FR-NTF): Risk,
Deviation, Notification, WebhookSubscription, ForesightThreshold,
QuietHoursConfig, this domain's own append-only foresight_audit_log, the
evidence-required deferred trigger on Deviation (same mechanism a895ae03ec5c
established for Commitment/Budget, f3a1c9d7e4b2 extended to DocumentVersion/
SpecClaim), and RLS on every new tenant-scoped table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '45309b1d751f'
down_revision: Union[str, Sequence[str], None] = 'a7c2e5f19b34'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("CREATE TYPE risk_source AS ENUM ('silence', 'contradiction', 'forecast');")
    op.execute("CREATE TYPE risk_severity AS ENUM ('low', 'medium', 'high', 'critical');")
    op.execute("CREATE TYPE risk_status AS ENUM ('open', 'acknowledged', 'resolved', 'superseded');")
    op.execute("CREATE TYPE deviation_status AS ENUM ('auto_drafted', 'confirmed', 'resolved');")
    op.execute("CREATE TYPE notification_channel AS ENUM ('webhook', 'push', 'email', 'teams');")
    op.execute(
        "CREATE TYPE foresight_threshold_metric AS ENUM "
        "('silence_multiplier', 'escalation_hours', 'forecast_slack_days');"
    )
    op.execute(
        "CREATE TYPE foresight_audit_action AS ENUM "
        "('risk_created', 'risk_superseded', 'risk_acknowledged', 'risk_escalated', 'risk_resolved', "
        "'deviation_drafted', 'deviation_confirmed', 'deviation_resolved', "
        "'notification_created', 'notification_delivered', 'commitment_auto_transitioned');"
    )

    # --- risks. superseded_by is self-referential — allowed inline in one
    # CREATE TABLE, same precedent spec_claims.contradicts already set
    # (f3a1c9d7e4b2) for a self-referencing FK declared in the same statement
    # as the table it points at.
    op.create_table(
        'risks',
        sa.Column('project_id', sa.UUID(), nullable=False, comment='RLS scope'),
        sa.Column('source', postgresql.ENUM('silence', 'contradiction', 'forecast', name='risk_source', create_type=False), nullable=False),
        sa.Column('finding_key', sa.String(), nullable=False, comment='stable dedup identity for this underlying condition'),
        sa.Column('severity', postgresql.ENUM('low', 'medium', 'high', 'critical', name='risk_severity', create_type=False), nullable=False),
        sa.Column('status', postgresql.ENUM('open', 'acknowledged', 'resolved', 'superseded', name='risk_status', create_type=False), nullable=False),
        sa.Column('commitment_id', sa.UUID(), nullable=True),
        sa.Column('milestone_id', sa.UUID(), nullable=True),
        sa.Column('spec_claim_id', sa.UUID(), nullable=True),
        sa.Column('downstream_consequence', sa.String(), nullable=False, comment='FR-FOR-09: NOT NULL by design'),
        sa.Column('base_rate', sa.Float(), nullable=True, comment="FR-FOR-02: null means 'no base rate yet'"),
        sa.Column('detail', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('acknowledged_by', sa.UUID(), nullable=True),
        sa.Column('acknowledged_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('escalated_to_role', postgresql.ENUM('project_manager', 'producer', 'finance', 'account_manager', 'designer', 'administrator', 'delegate', 'read_only', name='membership_role', create_type=False), nullable=True),
        sa.Column('escalated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('superseded_by', sa.UUID(), nullable=True),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['acknowledged_by'], ['users.id'], ),
        sa.ForeignKeyConstraint(['commitment_id'], ['commitments.id'], ),
        sa.ForeignKeyConstraint(['milestone_id'], ['milestones.id'], ),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
        sa.ForeignKeyConstraint(['spec_claim_id'], ['spec_claims.id'], ),
        sa.ForeignKeyConstraint(['superseded_by'], ['risks.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_risks_project_id'), 'risks', ['project_id'], unique=False)
    op.create_index(op.f('ix_risks_commitment_id'), 'risks', ['commitment_id'], unique=False)
    op.create_index(op.f('ix_risks_milestone_id'), 'risks', ['milestone_id'], unique=False)
    op.create_index(op.f('ix_risks_spec_claim_id'), 'risks', ['spec_claim_id'], unique=False)

    # --- deviations.
    op.create_table(
        'deviations',
        sa.Column('project_id', sa.UUID(), nullable=False, comment='RLS scope'),
        sa.Column('class_term_id', sa.UUID(), nullable=False, comment="ontology_terms row, category='deviation_class'"),
        sa.Column('status', postgresql.ENUM('auto_drafted', 'confirmed', 'resolved', name='deviation_status', create_type=False), nullable=False),
        sa.Column('description_en', sa.String(), nullable=False),
        sa.Column('risk_id', sa.UUID(), nullable=True, comment='FR-DEV-04: the Risk whose detection auto-drafted this row, if any'),
        sa.Column('commitment_id', sa.UUID(), nullable=True),
        sa.Column('milestone_id', sa.UUID(), nullable=True),
        sa.Column('resolution_date', sa.DateTime(timezone=True), nullable=True),
        sa.Column('resolution_owner', sa.UUID(), nullable=True),
        sa.Column('confirmed_by', sa.UUID(), nullable=True),
        sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('detail', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['class_term_id'], ['ontology_terms.id'], ),
        sa.ForeignKeyConstraint(['commitment_id'], ['commitments.id'], ),
        sa.ForeignKeyConstraint(['confirmed_by'], ['users.id'], ),
        sa.ForeignKeyConstraint(['milestone_id'], ['milestones.id'], ),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
        sa.ForeignKeyConstraint(['resolution_owner'], ['users.id'], ),
        sa.ForeignKeyConstraint(['risk_id'], ['risks.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_deviations_project_id'), 'deviations', ['project_id'], unique=False)
    op.create_index(op.f('ix_deviations_risk_id'), 'deviations', ['risk_id'], unique=False)
    op.create_index(op.f('ix_deviations_commitment_id'), 'deviations', ['commitment_id'], unique=False)
    op.create_index(op.f('ix_deviations_milestone_id'), 'deviations', ['milestone_id'], unique=False)

    # --- evidence extension (app/models/ledger.py's Evidence docstring):
    # a fifth nullable subject column, and the exactly-one-subject CHECK
    # widened from 4 to 5 terms — same shape f3a1c9d7e4b2 already used to
    # widen it from 2 to 4.
    op.add_column('evidence', sa.Column('deviation_id', sa.UUID(), nullable=True))
    op.create_index(op.f('ix_evidence_deviation_id'), 'evidence', ['deviation_id'], unique=False)
    op.create_foreign_key(
        'evidence_deviation_id_fkey', 'evidence', 'deviations', ['deviation_id'], ['id'],
    )
    op.drop_constraint('evidence_exactly_one_subject', 'evidence', type_='check')
    op.create_check_constraint(
        'evidence_exactly_one_subject', 'evidence',
        "(commitment_id IS NOT NULL)::int + (budget_id IS NOT NULL)::int "
        "+ (document_version_id IS NOT NULL)::int + (spec_claim_id IS NOT NULL)::int "
        "+ (deviation_id IS NOT NULL)::int = 1",
    )

    # --- Evidence-linkage enforcement on Deviation, same deferred-
    # constraint-trigger mechanism a895ae03ec5c/f3a1c9d7e4b2 already
    # established (FR-DEV-02).
    op.execute("""
        CREATE OR REPLACE FUNCTION check_deviation_has_evidence() RETURNS trigger AS $$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM evidence WHERE deviation_id = NEW.id) THEN
            RAISE EXCEPTION 'deviation % has no evidence (PRD §6.7, FR-DEV-02)', NEW.id;
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER deviation_requires_evidence
        AFTER INSERT OR UPDATE ON deviations
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION check_deviation_has_evidence();
    """)

    # --- notifications.
    op.create_table(
        'notifications',
        sa.Column('project_id', sa.UUID(), nullable=False, comment='RLS scope'),
        sa.Column('recipient_id', sa.UUID(), nullable=False),
        sa.Column('risk_id', sa.UUID(), nullable=True),
        sa.Column('deviation_id', sa.UUID(), nullable=True),
        sa.Column('commitment_id', sa.UUID(), nullable=True),
        sa.Column('collapsed_risk_ids', postgresql.ARRAY(sa.UUID()), nullable=False),
        sa.Column('collapsed_count', sa.Integer(), nullable=False),
        sa.Column('severity', postgresql.ENUM('low', 'medium', 'high', 'critical', name='risk_severity', create_type=False), nullable=False),
        sa.Column('downstream_consequence', sa.String(), nullable=False, comment='FR-NTF-02: NOT NULL by design'),
        sa.Column('deliverable_at', sa.DateTime(timezone=True), nullable=False, comment='FR-NTF-04: earliest instant quiet hours allow this to be sent'),
        sa.Column('delivered_via', postgresql.ENUM('webhook', 'push', 'email', 'teams', name='notification_channel', create_type=False), nullable=True),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('acknowledged_by', sa.UUID(), nullable=True),
        sa.Column('acknowledged_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('detail', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.CheckConstraint(
            '(risk_id IS NOT NULL) OR (deviation_id IS NOT NULL) OR (commitment_id IS NOT NULL)',
            name='notification_has_a_subject',
        ),
        sa.ForeignKeyConstraint(['acknowledged_by'], ['users.id'], ),
        sa.ForeignKeyConstraint(['commitment_id'], ['commitments.id'], ),
        sa.ForeignKeyConstraint(['deviation_id'], ['deviations.id'], ),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
        sa.ForeignKeyConstraint(['recipient_id'], ['users.id'], ),
        sa.ForeignKeyConstraint(['risk_id'], ['risks.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_notifications_project_id'), 'notifications', ['project_id'], unique=False)
    op.create_index(op.f('ix_notifications_recipient_id'), 'notifications', ['recipient_id'], unique=False)
    op.create_index(op.f('ix_notifications_risk_id'), 'notifications', ['risk_id'], unique=False)
    op.create_index(op.f('ix_notifications_deviation_id'), 'notifications', ['deviation_id'], unique=False)
    op.create_index(op.f('ix_notifications_commitment_id'), 'notifications', ['commitment_id'], unique=False)

    # --- webhook_subscriptions.
    op.create_table(
        'webhook_subscriptions',
        sa.Column('project_id', sa.UUID(), nullable=False, comment='RLS scope'),
        sa.Column('url', sa.String(), nullable=False),
        sa.Column('event_types', postgresql.ARRAY(sa.String()), nullable=False, comment="subset of {'commitment', 'risk', 'deviation'}"),
        sa.Column('secret', sa.String(), nullable=False, comment='HMAC-SHA256 signing key, generated at creation'),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('created_by', sa.UUID(), nullable=False),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_webhook_subscriptions_project_id'), 'webhook_subscriptions', ['project_id'], unique=False)

    # --- foresight_thresholds (org-scoped, same direct-organisation_id RLS
    # shape as retention_policies — FR-FOR-07 extends that config-table
    # pattern rather than inventing a parallel one).
    op.create_table(
        'foresight_thresholds',
        sa.Column('organisation_id', sa.UUID(), nullable=False, comment='RLS scope'),
        sa.Column('project_id', sa.UUID(), nullable=True, comment='NULL applies org-wide across every project'),
        sa.Column('deviation_class_term_id', sa.UUID(), nullable=True, comment='NULL applies to every deviation class'),
        sa.Column('metric', postgresql.ENUM('silence_multiplier', 'escalation_hours', 'forecast_slack_days', name='foresight_threshold_metric', create_type=False), nullable=False),
        sa.Column('value', sa.Float(), nullable=False),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['deviation_class_term_id'], ['ontology_terms.id'], ),
        sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'organisation_id', 'project_id', 'deviation_class_term_id', 'metric',
            name='foresight_thresholds_unique_scope', postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(op.f('ix_foresight_thresholds_organisation_id'), 'foresight_thresholds', ['organisation_id'], unique=False)
    op.create_index(op.f('ix_foresight_thresholds_project_id'), 'foresight_thresholds', ['project_id'], unique=False)

    # --- quiet_hours_configs.
    op.create_table(
        'quiet_hours_configs',
        sa.Column('project_id', sa.UUID(), nullable=False, comment='RLS scope'),
        sa.Column('quiet_start_local', sa.Time(), nullable=False, comment='local to Project.timezone'),
        sa.Column('quiet_end_local', sa.Time(), nullable=False, comment='local to Project.timezone'),
        sa.Column('critical_severity_threshold', postgresql.ENUM('low', 'medium', 'high', 'critical', name='risk_severity', create_type=False), nullable=False),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('project_id', name='quiet_hours_configs_one_per_project'),
    )

    # --- foresight_audit_log (this domain's own append-only trail).
    op.create_table(
        'foresight_audit_log',
        sa.Column('project_id', sa.UUID(), nullable=False, comment='RLS scope'),
        sa.Column('risk_id', sa.UUID(), nullable=True),
        sa.Column('deviation_id', sa.UUID(), nullable=True),
        sa.Column('notification_id', sa.UUID(), nullable=True),
        sa.Column('action', postgresql.ENUM('risk_created', 'risk_superseded', 'risk_acknowledged', 'risk_escalated', 'risk_resolved', 'deviation_drafted', 'deviation_confirmed', 'deviation_resolved', 'notification_created', 'notification_delivered', 'commitment_auto_transitioned', name='foresight_audit_action', create_type=False), nullable=False),
        sa.Column('actor_id', sa.UUID(), nullable=True),
        sa.Column('occurred_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('detail', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(['actor_id'], ['users.id'], ),
        sa.ForeignKeyConstraint(['deviation_id'], ['deviations.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['notification_id'], ['notifications.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
        sa.ForeignKeyConstraint(['risk_id'], ['risks.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_foresight_audit_log_project_id'), 'foresight_audit_log', ['project_id'], unique=False)
    op.create_index(op.f('ix_foresight_audit_log_risk_id'), 'foresight_audit_log', ['risk_id'], unique=False)
    op.create_index(op.f('ix_foresight_audit_log_deviation_id'), 'foresight_audit_log', ['deviation_id'], unique=False)
    op.create_index(op.f('ix_foresight_audit_log_notification_id'), 'foresight_audit_log', ['notification_id'], unique=False)

    op.execute("ALTER TABLE foresight_audit_log ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE foresight_audit_log FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY tenant_isolation ON foresight_audit_log
          USING (project_id IN (
            SELECT id FROM projects
            WHERE organisation_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
          ));
    """)
    # Append-only, with the SET-NULL-on-delete carve-out built in from the
    # start (e771d0318751's lesson, already applied up front by
    # document_audit_log — same here).
    op.execute("""
        CREATE OR REPLACE FUNCTION forbid_foresight_audit_log_mutation() RETURNS trigger AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'foresight_audit_log is append-only: DELETE not permitted';
          END IF;
          IF NEW.project_id IS DISTINCT FROM OLD.project_id
             OR NEW.action IS DISTINCT FROM OLD.action
             OR NEW.actor_id IS DISTINCT FROM OLD.actor_id
             OR NEW.occurred_at IS DISTINCT FROM OLD.occurred_at
             OR NEW.detail IS DISTINCT FROM OLD.detail
             OR (NEW.risk_id IS DISTINCT FROM OLD.risk_id AND NEW.risk_id IS NOT NULL)
             OR (NEW.deviation_id IS DISTINCT FROM OLD.deviation_id AND NEW.deviation_id IS NOT NULL)
             OR (NEW.notification_id IS DISTINCT FROM OLD.notification_id AND NEW.notification_id IS NOT NULL)
          THEN
            RAISE EXCEPTION 'foresight_audit_log is append-only except SET NULL on delete: only risk_id/deviation_id/notification_id may be nulled';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER foresight_audit_log_append_only
        BEFORE UPDATE OR DELETE ON foresight_audit_log
        FOR EACH ROW EXECUTE FUNCTION forbid_foresight_audit_log_mutation();
    """)

    # --- RLS on the remaining tenant-scoped tables (CUE-Tech-Stack.md P1) —
    # risks/deviations/notifications/webhook_subscriptions/
    # quiet_hours_configs all carry a direct project_id column, same
    # direct-column shape as commitments/documents; foresight_thresholds is
    # org-scoped, same direct-organisation_id shape as retention_policies.
    for table in ('risks', 'deviations', 'notifications', 'webhook_subscriptions', 'quiet_hours_configs'):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
        op.execute(f"""
            CREATE POLICY tenant_isolation ON {table}
              USING (project_id IN (
                SELECT id FROM projects
                WHERE organisation_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
              ));
        """)

    op.execute("ALTER TABLE foresight_thresholds ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE foresight_thresholds FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY tenant_isolation ON foresight_thresholds
          USING (organisation_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid);
    """)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON foresight_thresholds;")
    op.execute("ALTER TABLE foresight_thresholds DISABLE ROW LEVEL SECURITY;")
    for table in ('quiet_hours_configs', 'webhook_subscriptions', 'notifications', 'deviations', 'risks'):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table};")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;")

    op.execute("DROP TRIGGER IF EXISTS foresight_audit_log_append_only ON foresight_audit_log;")
    op.execute("DROP FUNCTION IF EXISTS forbid_foresight_audit_log_mutation();")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON foresight_audit_log;")
    op.execute("ALTER TABLE foresight_audit_log DISABLE ROW LEVEL SECURITY;")
    op.drop_index(op.f('ix_foresight_audit_log_notification_id'), table_name='foresight_audit_log')
    op.drop_index(op.f('ix_foresight_audit_log_deviation_id'), table_name='foresight_audit_log')
    op.drop_index(op.f('ix_foresight_audit_log_risk_id'), table_name='foresight_audit_log')
    op.drop_index(op.f('ix_foresight_audit_log_project_id'), table_name='foresight_audit_log')
    op.drop_table('foresight_audit_log')

    op.drop_table('quiet_hours_configs')

    op.drop_index(op.f('ix_foresight_thresholds_project_id'), table_name='foresight_thresholds')
    op.drop_index(op.f('ix_foresight_thresholds_organisation_id'), table_name='foresight_thresholds')
    op.drop_table('foresight_thresholds')

    op.drop_index(op.f('ix_webhook_subscriptions_project_id'), table_name='webhook_subscriptions')
    op.drop_table('webhook_subscriptions')

    op.drop_index(op.f('ix_notifications_commitment_id'), table_name='notifications')
    op.drop_index(op.f('ix_notifications_deviation_id'), table_name='notifications')
    op.drop_index(op.f('ix_notifications_risk_id'), table_name='notifications')
    op.drop_index(op.f('ix_notifications_recipient_id'), table_name='notifications')
    op.drop_index(op.f('ix_notifications_project_id'), table_name='notifications')
    op.drop_table('notifications')

    op.execute("DROP TRIGGER IF EXISTS deviation_requires_evidence ON deviations;")
    op.execute("DROP FUNCTION IF EXISTS check_deviation_has_evidence();")

    op.drop_constraint('evidence_exactly_one_subject', 'evidence', type_='check')
    op.create_check_constraint(
        'evidence_exactly_one_subject', 'evidence',
        "(commitment_id IS NOT NULL)::int + (budget_id IS NOT NULL)::int "
        "+ (document_version_id IS NOT NULL)::int + (spec_claim_id IS NOT NULL)::int = 1",
    )
    op.drop_constraint('evidence_deviation_id_fkey', 'evidence', type_='foreignkey')
    op.drop_index(op.f('ix_evidence_deviation_id'), table_name='evidence')
    op.drop_column('evidence', 'deviation_id')

    op.drop_index(op.f('ix_deviations_milestone_id'), table_name='deviations')
    op.drop_index(op.f('ix_deviations_commitment_id'), table_name='deviations')
    op.drop_index(op.f('ix_deviations_risk_id'), table_name='deviations')
    op.drop_index(op.f('ix_deviations_project_id'), table_name='deviations')
    op.drop_table('deviations')

    op.drop_index(op.f('ix_risks_spec_claim_id'), table_name='risks')
    op.drop_index(op.f('ix_risks_milestone_id'), table_name='risks')
    op.drop_index(op.f('ix_risks_commitment_id'), table_name='risks')
    op.drop_index(op.f('ix_risks_project_id'), table_name='risks')
    op.drop_table('risks')

    op.execute("DROP TYPE IF EXISTS foresight_audit_action;")
    op.execute("DROP TYPE IF EXISTS foresight_threshold_metric;")
    op.execute("DROP TYPE IF EXISTS notification_channel;")
    op.execute("DROP TYPE IF EXISTS deviation_status;")
    op.execute("DROP TYPE IF EXISTS risk_status;")
    op.execute("DROP TYPE IF EXISTS risk_severity;")
    op.execute("DROP TYPE IF EXISTS risk_source;")
