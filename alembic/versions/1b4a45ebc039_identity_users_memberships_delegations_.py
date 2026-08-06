"""identity: users, memberships, delegations, RBAC

Revision ID: 1b4a45ebc039
Revises: d05dce0f415d
Create Date: 2026-08-07 05:34:24.749941

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '1b4a45ebc039'
down_revision: Union[str, Sequence[str], None] = 'd05dce0f415d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

membership_role = sa.Enum(
    'project_manager', 'producer', 'finance', 'account_manager', 'designer',
    'administrator', 'delegate', 'read_only',
    name='membership_role',
)


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'users',
        sa.Column('organisation_id', sa.UUID(), nullable=False),
        sa.Column('issuer', sa.String(), nullable=False, comment="token 'iss' claim — which provider vouched for this user"),
        sa.Column('external_subject', sa.String(), nullable=False, comment="token 'sub' claim — stable per issuer"),
        sa.Column('email', sa.String(), nullable=False),
        sa.Column('display_name', sa.String(), nullable=True),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('issuer', 'external_subject', name='users_issuer_subject_key'),
        sa.UniqueConstraint('organisation_id', 'email', name='users_org_email_key'),
    )
    op.create_index(op.f('ix_users_organisation_id'), 'users', ['organisation_id'], unique=False)

    op.create_table(
        'memberships',
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('project_id', sa.UUID(), nullable=False),
        sa.Column('role', membership_role, nullable=False),
        sa.Column('granted_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('granted_by', sa.UUID(), nullable=True),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(['granted_by'], ['users.id'], ),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'project_id', name='memberships_user_project_key'),
    )
    op.create_index(op.f('ix_memberships_project_id'), 'memberships', ['project_id'], unique=False)
    op.create_index(op.f('ix_memberships_user_id'), 'memberships', ['user_id'], unique=False)

    op.create_table(
        'delegations',
        sa.Column('project_id', sa.UUID(), nullable=False),
        sa.Column('delegator_id', sa.UUID(), nullable=False, comment='whose access is being lent'),
        sa.Column('delegate_id', sa.UUID(), nullable=False, comment='who temporarily receives it'),
        sa.Column('role', membership_role, nullable=False, comment="the role's access being delegated"),
        sa.Column('granted_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('granted_by', sa.UUID(), nullable=False, comment='who authorised the grant — delegator or an administrator'),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('revoked_by', sa.UUID(), nullable=True),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.CheckConstraint('delegate_id != delegator_id', name='delegation_not_self'),
        sa.CheckConstraint('expires_at > granted_at', name='delegation_expires_after_grant'),
        sa.ForeignKeyConstraint(['delegate_id'], ['users.id'], ),
        sa.ForeignKeyConstraint(['delegator_id'], ['users.id'], ),
        sa.ForeignKeyConstraint(['granted_by'], ['users.id'], ),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
        sa.ForeignKeyConstraint(['revoked_by'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_delegations_delegate_id'), 'delegations', ['delegate_id'], unique=False)
    op.create_index(op.f('ix_delegations_delegator_id'), 'delegations', ['delegator_id'], unique=False)
    op.create_index(op.f('ix_delegations_project_id'), 'delegations', ['project_id'], unique=False)

    # --- Fields the earlier ledger/audit sessions left as "FK to users table,
    # deferred to identity module" (app/models/ledger.py, app/models/audit.py)
    # — now that the table exists, wire the real constraint rather than
    # leaving a bare UUID column trusting the application to only ever put a
    # real user id there.
    op.create_foreign_key(
        'commitments_verified_by_fkey', 'commitments', 'users', ['verified_by'], ['id']
    )
    op.create_foreign_key(
        'budgets_approved_by_fkey', 'budgets', 'users', ['approved_by'], ['id']
    )
    op.create_foreign_key(
        'audit_log_actor_id_fkey', 'audit_log', 'users', ['actor_id'], ['id']
    )

    # --- Row-level tenant isolation, same pattern as a895ae03ec5c /
    # d05dce0f415d — every new tenant-scoped table gets it, per CLAUDE.md's
    # "verified in code, not trusted" posture applied to RLS coverage itself
    # (Prompt 3's own instructions: an unprotected table here is "an
    # unprotected hole in an otherwise-enforced model").
    op.execute("ALTER TABLE users ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE users FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY tenant_isolation ON users
          USING (organisation_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid);
    """)
    op.execute("ALTER TABLE memberships ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE memberships FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY tenant_isolation ON memberships
          USING (project_id IN (
            SELECT id FROM projects
            WHERE organisation_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
          ));
    """)
    op.execute("ALTER TABLE delegations ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE delegations FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY tenant_isolation ON delegations
          USING (project_id IN (
            SELECT id FROM projects
            WHERE organisation_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
          ));
    """)

    # --- FR-ADM-04: delegations is its own audit trail (see
    # app/identity/models.py's Delegation docstring for why this table, not a
    # separate log, satisfies "who granted what to whom, when, and when it
    # expired") — enforced the same way audit_log's append-only-ness is
    # (CLAUDE.md: not trusted from application code alone). The one
    # legitimate mutation is early revocation (revoked_at/revoked_by); every
    # other column, and DELETE entirely, is rejected.
    op.execute("""
        CREATE OR REPLACE FUNCTION forbid_delegation_mutation() RETURNS trigger AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'delegations is append-only (FR-ADM-04): DELETE not permitted';
          END IF;
          IF NEW.delegator_id IS DISTINCT FROM OLD.delegator_id
             OR NEW.delegate_id IS DISTINCT FROM OLD.delegate_id
             OR NEW.project_id IS DISTINCT FROM OLD.project_id
             OR NEW.role IS DISTINCT FROM OLD.role
             OR NEW.granted_at IS DISTINCT FROM OLD.granted_at
             OR NEW.granted_by IS DISTINCT FROM OLD.granted_by
             OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
          THEN
            RAISE EXCEPTION 'delegations is append-only except revocation (FR-ADM-04): only revoked_at/revoked_by may change';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER delegation_append_only
        BEFORE UPDATE OR DELETE ON delegations
        FOR EACH ROW EXECUTE FUNCTION forbid_delegation_mutation();
    """)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP TRIGGER IF EXISTS delegation_append_only ON delegations;")
    op.execute("DROP FUNCTION IF EXISTS forbid_delegation_mutation();")

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON delegations;")
    op.execute("ALTER TABLE delegations DISABLE ROW LEVEL SECURITY;")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON memberships;")
    op.execute("ALTER TABLE memberships DISABLE ROW LEVEL SECURITY;")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON users;")
    op.execute("ALTER TABLE users DISABLE ROW LEVEL SECURITY;")

    op.drop_constraint('audit_log_actor_id_fkey', 'audit_log', type_='foreignkey')
    op.drop_constraint('budgets_approved_by_fkey', 'budgets', type_='foreignkey')
    op.drop_constraint('commitments_verified_by_fkey', 'commitments', type_='foreignkey')

    op.drop_index(op.f('ix_delegations_project_id'), table_name='delegations')
    op.drop_index(op.f('ix_delegations_delegator_id'), table_name='delegations')
    op.drop_index(op.f('ix_delegations_delegate_id'), table_name='delegations')
    op.drop_table('delegations')

    op.drop_index(op.f('ix_memberships_user_id'), table_name='memberships')
    op.drop_index(op.f('ix_memberships_project_id'), table_name='memberships')
    op.drop_table('memberships')

    op.drop_index(op.f('ix_users_organisation_id'), table_name='users')
    op.drop_table('users')

    op.execute("DROP TYPE IF EXISTS membership_role;")
