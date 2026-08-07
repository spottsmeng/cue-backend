"""twin archetype tenant scoping and explicit dependency edges

Revision ID: fbea5e75b4d8
Revises: eed5a4da79f6
Create Date: 2026-08-07 17:24:58.646772

"""
import uuid
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from seed_data.event_production_archetype import ARCHETYPE_CODE, ARCHETYPE_DEPENDENCIES
from seed_data.verticals import PLATFORM_VERTICALS

# revision identifiers, used by Alembic.
revision: str = 'fbea5e75b4d8'
down_revision: Union[str, Sequence[str], None] = 'eed5a4da79f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

EVENT_PRODUCTION_CODE = next(code for code, _name in PLATFORM_VERTICALS if code == "event-production")


def upgrade() -> None:
    # --- Give MilestoneArchetype the same two-column layering OntologyTerm
    # already has (CUE-PRD.md §4.2.1) — see app/twin/models.py's
    # MilestoneArchetype docstring for why resolution differs from the
    # ontology's own (most-specific-wins, not a union).
    op.add_column(
        'milestone_archetypes', sa.Column('organisation_id', sa.UUID(), nullable=True)
    )
    op.create_foreign_key(
        'milestone_archetypes_organisation_id_fkey', 'milestone_archetypes', 'organisations',
        ['organisation_id'], ['id'],
    )
    op.create_index(
        op.f('ix_milestone_archetypes_organisation_id'), 'milestone_archetypes', ['organisation_id'],
        unique=False,
    )

    # --- The template's own dependency graph — see app/twin/models.py's
    # MilestoneArchetypeDependency docstring. Same shape as the live
    # `dependencies` table (616998fb7f1b), deliberately, so materialization
    # is a straight copy.
    op.create_table(
        'milestone_archetype_dependencies',
        sa.Column('archetype_id', sa.UUID(), nullable=False),
        sa.Column('upstream_item_id', sa.UUID(), nullable=False),
        sa.Column('downstream_item_id', sa.UUID(), nullable=False),
        sa.Column('lag_days', sa.Integer(), nullable=False),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.CheckConstraint(
            'upstream_item_id != downstream_item_id', name='archetype_dependency_not_self'
        ),
        sa.CheckConstraint('lag_days >= 0', name='archetype_dependency_lag_days_non_negative'),
        sa.ForeignKeyConstraint(['archetype_id'], ['milestone_archetypes.id'], ),
        sa.ForeignKeyConstraint(['downstream_item_id'], ['milestone_archetype_items.id'], ),
        sa.ForeignKeyConstraint(['upstream_item_id'], ['milestone_archetype_items.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'upstream_item_id', 'downstream_item_id', name='archetype_dependency_edge_unique'
        ),
    )
    op.create_index(
        op.f('ix_milestone_archetype_dependencies_archetype_id'), 'milestone_archetype_dependencies',
        ['archetype_id'], unique=False,
    )
    op.create_index(
        op.f('ix_milestone_archetype_dependencies_upstream_item_id'), 'milestone_archetype_dependencies',
        ['upstream_item_id'], unique=False,
    )
    op.create_index(
        op.f('ix_milestone_archetype_dependencies_downstream_item_id'), 'milestone_archetype_dependencies',
        ['downstream_item_id'], unique=False,
    )

    conn = op.get_bind()

    # --- Re-key the default archetype onto the vertical-pack tier, same
    # reasoning as eed5a4da79f6 for milestone_type terms: it was seeded
    # NULL/universal (9b2f8bc21d89) back when one vertical existed, which
    # stops being a simplification once a second one is real roadmap.
    vertical_id = conn.execute(
        sa.text("SELECT id FROM verticals WHERE code = :code"), {"code": EVENT_PRODUCTION_CODE}
    ).scalar_one()
    conn.execute(
        sa.text("UPDATE milestone_archetypes SET vertical_id = :vid WHERE code = :code"),
        {"vid": vertical_id, "code": ARCHETYPE_CODE},
    )

    # --- Seed this archetype's dependency edges (previously only implied by
    # MilestoneArchetypeItem.sequence_order at materialization time — see
    # that model's docstring for why an explicit edge table replaces it).
    archetype_id = conn.execute(
        sa.text("SELECT id FROM milestone_archetypes WHERE code = :code"), {"code": ARCHETYPE_CODE}
    ).scalar_one()
    item_ids_by_code = dict(
        conn.execute(
            sa.text("SELECT type_code, id FROM milestone_archetype_items WHERE archetype_id = :aid"),
            {"aid": archetype_id},
        ).all()
    )
    conn.execute(
        sa.text(
            "INSERT INTO milestone_archetype_dependencies "
            "(id, archetype_id, upstream_item_id, downstream_item_id, lag_days) "
            "VALUES (:id, :archetype_id, :upstream_id, :downstream_id, :lag_days)"
        ),
        [
            {
                "id": uuid.uuid4(),
                "archetype_id": archetype_id,
                "upstream_id": item_ids_by_code[upstream_code],
                "downstream_id": item_ids_by_code[downstream_code],
                "lag_days": lag_days,
            }
            for upstream_code, downstream_code, lag_days in ARCHETYPE_DEPENDENCIES
        ],
    )


def downgrade() -> None:
    op.execute(
        f"DELETE FROM milestone_archetype_dependencies WHERE archetype_id IN "
        f"(SELECT id FROM milestone_archetypes WHERE code = '{ARCHETYPE_CODE}');"
    )
    op.execute(
        f"UPDATE milestone_archetypes SET vertical_id = NULL WHERE code = '{ARCHETYPE_CODE}';"
    )

    op.drop_index(
        op.f('ix_milestone_archetype_dependencies_downstream_item_id'),
        table_name='milestone_archetype_dependencies',
    )
    op.drop_index(
        op.f('ix_milestone_archetype_dependencies_upstream_item_id'),
        table_name='milestone_archetype_dependencies',
    )
    op.drop_index(
        op.f('ix_milestone_archetype_dependencies_archetype_id'),
        table_name='milestone_archetype_dependencies',
    )
    op.drop_table('milestone_archetype_dependencies')

    op.drop_index(op.f('ix_milestone_archetypes_organisation_id'), table_name='milestone_archetypes')
    op.drop_constraint(
        'milestone_archetypes_organisation_id_fkey', 'milestone_archetypes', type_='foreignkey'
    )
    op.drop_column('milestone_archetypes', 'organisation_id')
