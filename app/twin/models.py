import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, Enum, ForeignKey, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base, TZDateTime, UUIDPk

# Twin-domain models (PRD §6.8, FR-TWN) live here, not in app/models/ledger.py
# — that file's own top-of-file scope note explicitly carves Dependency (and
# Meeting/Document/Risk/Deviation/Membership) out of the Ledger core, the same
# way Membership/Delegation/User ended up in app/identity/models.py rather
# than bolted onto ledger.py when their turn came. Milestone itself stays in
# ledger.py (moving it is out of this session's scope), but nothing new about
# the Twin domain gets added there.


class Dependency(Base, UUIDPk):
    """PRD §4.1: MILESTONE ||--o{ DEPENDENCY (upstream_of / downstream_of).

    `lag_days` is FR-TWN-10's "duration" — the minimum gap the CPM engine
    (app/twin/graph.py) enforces between the upstream milestone's date and the
    downstream milestone's date; a PM override of "how long this dependency
    takes" is an edit to this column, not a new row.
    """

    __tablename__ = "dependencies"
    __table_args__ = (
        CheckConstraint("upstream_milestone_id != downstream_milestone_id", name="dependency_not_self"),
        CheckConstraint("lag_days >= 0", name="dependency_lag_days_non_negative"),
        UniqueConstraint(
            "upstream_milestone_id", "downstream_milestone_id", name="dependency_edge_unique"
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), index=True, comment="RLS scope"
    )
    upstream_milestone_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("milestones.id"), index=True
    )
    downstream_milestone_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("milestones.id"), index=True
    )
    lag_days: Mapped[int] = mapped_column(default=0)


class MilestoneArchetype(Base, UUIDPk):
    """FR-TWN-09's expert-seeded default(s), as reusable templates a
    project's milestones get seeded FROM (app/api/projects.py's
    create_project) — deliberately not a Python-side constant baked straight
    into the project-creation code path. Reference data belongs in the
    database, same as OntologyTerm, precisely so it can be *seeded by a
    migration* rather than requiring a code deploy to correct an offset, and
    so a second archetype is an additive row later, not a rewrite — the same
    append-only-reference-data posture CUE-PRD.md §4.2.1 already commits to
    for ontology_terms.

    Layered exactly like OntologyTerm (`vertical_id` + `organisation_id`,
    CUE-PRD.md §4.2.1) — but resolved differently, because an archetype is a
    whole coherent template, not a vocabulary word: OntologyTerm's effective
    set for a project is the *union* of all three tiers (every applicable
    term usable at once); an archetype resolution picks the *single* most
    specific match (a tenant's own default, if one exists, beats the
    vertical's default, which beats a bare universal default) — see
    app/twin/service.py's `_resolve_archetype`. Both NULL is the universal
    fallback every vertical can seed from if no vertical-specific archetype
    exists yet; `organisation_id` is empty at v1 (no tenant-authored
    archetype UI exists), same "mechanism exists, not a v1 feature
    commitment" posture CUE-PRD.md §4.2.1 already states for the ontology's
    own tenant-extension tier.

    A project's *actual* Milestone/Dependency rows are still a literal,
    per-project copy materialized at creation time (app/twin/service.py's
    materialize_archetype) — this table is never queried again after that,
    and a PM override (FR-TWN-10) always lands on the project's own copy,
    never back on the shared template. The alternative considered and
    rejected — skip the template table, hardcode the sequence directly in
    create_project — would have made a future second archetype a code change
    instead of a migration, for no offsetting simplicity: the per-project
    materialization step is required either way, since Milestone/Dependency
    rows cannot exist before a project does.
    """

    __tablename__ = "milestone_archetypes"

    code: Mapped[str] = mapped_column(unique=True, comment="stable key, e.g. 'event-production-default'")
    vertical_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("verticals.id"), default=None, index=True
    )
    organisation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organisations.id"), default=None, index=True
    )
    name: Mapped[str] = mapped_column()
    is_default: Mapped[bool] = mapped_column(
        default=False,
        comment="the fallback create_project seeds from at this tier when no archetype_code is given",
    )


class MilestoneArchetypeItem(Base, UUIDPk):
    """One ordered step in a MilestoneArchetype (PRD §6.8's paragraph after
    the FR-TWN table: Annex A's Project Timeline + Operation Schedule &
    Overtime, converted into "an ordered milestone sequence with relative
    day-offsets from event date").

    `type_code` is a plain string matching ontology_terms.code (category=
    'milestone_type'), deliberately NOT a foreign key to a specific
    ontology_terms.id row. OntologyTerm.code's own comment calls it a "stable
    machine key, never renamed once referenced" — the code is exactly the
    thing meant to be referenced durably; the row backing it can be
    versioned/superseded (OntologyTerm.version/effective_to) without this
    template needing a migration to follow along. It also keeps this table
    off the TRUNCATE-cascade graph that ontology_terms sits on (ontology_terms
    is swept and re-seeded with fresh ids on every test's cleanup — see
    tests/conftest.py — which a real FK here would have inherited).

    `sequence_order` is display/authoring order only — the actual dependency
    structure a project materializes from is MilestoneArchetypeDependency,
    below, not implied from this column. An earlier version of this table
    had materialization infer a straight chain (item N depends on N-1); that
    made the *template* incapable of expressing anything a future archetype
    with real parallel tracks (e.g. a gala's F&B/staging/AV running
    concurrently) would need, even though the *project-level* Dependency
    table was already a real graph — an asymmetry with no reason to exist
    once a second archetype was actually going to be built. Today's seed
    data (seed_data/event_production_archetype.py) still only contains a
    linear chain, honestly, because Annex A only gives us a schedule, not a
    branching graph — but the schema no longer has to change when the next
    archetype's does.
    """

    __tablename__ = "milestone_archetype_items"
    __table_args__ = (
        UniqueConstraint("archetype_id", "sequence_order", name="archetype_item_sequence_unique"),
    )

    archetype_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("milestone_archetypes.id"), index=True
    )
    sequence_order: Mapped[int] = mapped_column()
    type_code: Mapped[str] = mapped_column(comment="ontology_terms.code, category='milestone_type'")
    name: Mapped[str] = mapped_column(comment="seeds Milestone.name at materialization")
    day_offset: Mapped[int] = mapped_column(
        comment="relative to the project's event_start; negative = before, 0 = event day"
    )
    is_fixed: Mapped[bool] = mapped_column(
        default=False, comment="FR-TWN-11: seeds Milestone.is_fixed, e.g. 'doors'"
    )


class MilestoneArchetypeDependency(Base, UUIDPk):
    """The template's real dependency graph — same (upstream, downstream,
    lag_days) shape the live `Dependency` model uses, deliberately, so
    materialization (app/twin/service.py) is a straight copy: for each edge
    here, create a `Dependency` row between the two corresponding freshly-
    created Milestone rows. `archetype_id` is redundant with what the two
    items already imply (same convenience-scoping the live `Dependency`
    table already applies to `project_id` vs. deriving it through
    `milestones`), kept for the same reason: trivial "list this archetype's
    edges" queries without a join.
    """

    __tablename__ = "milestone_archetype_dependencies"
    __table_args__ = (
        CheckConstraint("upstream_item_id != downstream_item_id", name="archetype_dependency_not_self"),
        CheckConstraint("lag_days >= 0", name="archetype_dependency_lag_days_non_negative"),
        UniqueConstraint(
            "upstream_item_id", "downstream_item_id", name="archetype_dependency_edge_unique"
        ),
    )

    archetype_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("milestone_archetypes.id"), index=True
    )
    upstream_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("milestone_archetype_items.id"), index=True
    )
    downstream_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("milestone_archetype_items.id"), index=True
    )
    lag_days: Mapped[int] = mapped_column(default=0)


TwinAuditAction = Enum(
    "milestone_override", "dependency_override", "recompute",
    "milestone_created", "milestone_deleted", "dependency_created", "dependency_deleted",
    name="twin_audit_action",
)


class TwinAuditLog(Base, UUIDPk):
    """FR-TWN-10's "recorded as a project-specific adjustment" and PRD §11.1's
    audit posture, for the Twin domain specifically.

    Deliberately a separate table from app/models/audit.py's AuditLog, not a
    generalisation of it — that table's own docstring already says why:
    "Scoped to commitment mutations only for this build slice (Twin/.../ is a
    later phase) — commitment_id is NOT NULL rather than a generalised
    polymorphic subject, since a generalised shape isn't needed yet." That
    phase is this session; the generalisation still isn't needed — a second,
    equally narrow table costs nothing that a wider polymorphic redesign of
    the existing one would have risked (touching commitment audit code that
    already works and is already tested).
    """

    __tablename__ = "twin_audit_log"
    __table_args__ = (
        CheckConstraint(
            "NOT (milestone_id IS NOT NULL AND dependency_id IS NOT NULL)",
            name="twin_audit_log_not_both_subjects",
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), index=True, comment="RLS scope"
    )
    milestone_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("milestones.id", ondelete="SET NULL"), default=None, index=True,
        comment="SET NULL on delete: the audit entry outlives the milestone it documents",
    )
    dependency_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dependencies.id", ondelete="SET NULL"), default=None, index=True,
    )
    action: Mapped[str] = mapped_column(TwinAuditAction)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), default=None
    )
    occurred_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
