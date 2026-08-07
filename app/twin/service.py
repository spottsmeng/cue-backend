"""The DB-facing half of the Twin (FR-TWN) — loads a project's Milestone/
Dependency rows into app/twin/graph.py's plain dataclasses, materializes the
default archetype into a new project, and wires recomputation into a
commitment state transition. app/twin/graph.py itself never imports
SQLAlchemy; everything that does lives here.
"""

import uuid
from datetime import datetime, timedelta, timezone as dt_timezone

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Commitment, Deliverable, Milestone, OntologyTerm, Project
from app.twin.audit import record_twin_audit_event
from app.twin.graph import (
    DependencyEdge,
    MilestoneNode,
    PropagationResult,
    TwinResult,
    compute_twin,
    propagate,
)
from app.twin.models import Dependency, MilestoneArchetype, MilestoneArchetypeDependency, MilestoneArchetypeItem


class UnknownArchetypeCode(LookupError):
    """A caller-supplied `archetype_code` doesn't match any template — a
    routine client-input error (a typo, a code from a different org),
    distinct from the plain LookupError below for a missing
    milestone_type ontology term, which is an internal seed-data bug, not
    something a caller can trigger by choice. Kept as a separate type so
    app/api/projects.py can 422 one and let the other surface as a 500."""


async def _load_milestones_and_dependencies(
    session: AsyncSession, project_id: uuid.UUID
) -> tuple[list[Milestone], list[Dependency]]:
    milestones = (
        await session.execute(select(Milestone).where(Milestone.project_id == project_id))
    ).scalars().all()
    dependencies = (
        await session.execute(select(Dependency).where(Dependency.project_id == project_id))
    ).scalars().all()
    return list(milestones), list(dependencies)


def _to_graph(
    milestones: list[Milestone], dependencies: list[Dependency]
) -> tuple[list[MilestoneNode], list[DependencyEdge]]:
    nodes = [
        MilestoneNode(id=m.id, planned_at=m.planned_at, actual_at=m.actual_at, is_fixed=m.is_fixed)
        for m in milestones
    ]
    edges = [
        DependencyEdge(
            upstream_id=d.upstream_milestone_id,
            downstream_id=d.downstream_milestone_id,
            lag_days=d.lag_days,
        )
        for d in dependencies
    ]
    return nodes, edges


async def build_graph(
    session: AsyncSession, project_id: uuid.UUID
) -> tuple[list[MilestoneNode], list[DependencyEdge]]:
    """Public entry point for callers that need the graph dataclasses
    without running a full CPM computation — app/api/milestones.py's
    create_dependency uses this to check a candidate edge for cycles before
    inserting it."""
    milestones, dependencies = await _load_milestones_and_dependencies(session, project_id)
    return _to_graph(milestones, dependencies)


async def compute_current(session: AsyncSession, project_id: uuid.UUID) -> TwinResult:
    """FR-TWN-03. Recomputed fresh from the current Milestone/Dependency rows
    on every call — there is no cached snapshot to go stale, which is what
    actually satisfies FR-TWN-04's 10-second bar (nothing to wait on)."""
    nodes, edges = await build_graph(session, project_id)
    return compute_twin(nodes, edges)


async def compute_propagation(
    session: AsyncSession, project_id: uuid.UUID, milestone_id: uuid.UUID, new_date: datetime
) -> PropagationResult:
    nodes, edges = await build_graph(session, project_id)
    return propagate(nodes, edges, milestone_id, new_date)


def _most_specific(organisation_id: uuid.UUID | None, vertical_id: uuid.UUID | None) -> int:
    """Shared ranking for both archetype and ontology-term resolution
    (CUE-PRD.md §4.2.1's three tiers): a tenant-extension row beats a
    vertical-pack row, which beats a universal-core row. Ontology *unions*
    every tier's matches; archetype resolution picks the single highest-
    ranked one — see app/twin/models.py's MilestoneArchetype docstring for
    why those differ."""
    if organisation_id is not None:
        return 2
    if vertical_id is not None:
        return 1
    return 0


async def _resolve_ontology_terms(
    session: AsyncSession, category: str, project: Project
) -> dict[str, OntologyTerm]:
    """CUE-PRD.md §4.2.1: "a project's effective ontology for a category is
    the union of all three [tiers]... resolved at query time" — a term code
    that exists at more than one tier resolves to its most specific row."""
    rows = (
        await session.execute(
            select(OntologyTerm).where(
                OntologyTerm.category == category,
                or_(
                    and_(OntologyTerm.vertical_id.is_(None), OntologyTerm.organisation_id.is_(None)),
                    and_(
                        OntologyTerm.vertical_id == project.vertical_id,
                        OntologyTerm.organisation_id.is_(None),
                    ),
                    # Tenant extension, PRD §4.2.1's own wording: "one
                    # organisation's own additions WITHIN THEIR VERTICAL" —
                    # matching organisation_id alone, with no vertical_id
                    # constraint, would leak an org's renovation-vertical
                    # term into their event-production project.
                    and_(
                        OntologyTerm.vertical_id == project.vertical_id,
                        OntologyTerm.organisation_id == project.organisation_id,
                    ),
                ),
            )
        )
    ).scalars().all()

    by_code: dict[str, OntologyTerm] = {}
    for term in rows:
        current = by_code.get(term.code)
        if current is None or _most_specific(term.organisation_id, term.vertical_id) > _most_specific(
            current.organisation_id, current.vertical_id
        ):
            by_code[term.code] = term
    return by_code


async def get_milestone_type_term(session: AsyncSession, project: Project, code: str) -> OntologyTerm:
    """Public single-code lookup for app/api/milestones.py's create_milestone
    — same resolution _resolve_ontology_terms uses for the whole archetype,
    exposed for the one-off case of a caller adding a single milestone the
    archetype didn't seed."""
    terms = await _resolve_ontology_terms(session, "milestone_type", project)
    term = terms.get(code)
    if term is None:
        raise LookupError(f"milestone_type ontology term not found for this project: {code!r}")
    return term


async def _resolve_archetype(
    session: AsyncSession, project: Project, archetype_code: str | None
) -> MilestoneArchetype | None:
    """An explicit `archetype_code` always wins (a PM picking a specific
    template from a gallery); otherwise the most specific `is_default` row
    for this project's tier wins — a tenant's own default, if one exists,
    beats the vertical's, which beats the bare universal one. Returns None
    only if nothing at all matches (no archetype seeded for this vertical
    and no universal fallback either) — materialize_archetype treats that as
    "nothing to seed," not an error, since FR-TWN-01's graph requirement is
    satisfied by an empty-but-real graph too.
    """
    if archetype_code is not None:
        archetype = (
            await session.execute(
                select(MilestoneArchetype).where(MilestoneArchetype.code == archetype_code)
            )
        ).scalar_one_or_none()
        if archetype is None:
            raise UnknownArchetypeCode(f"unknown archetype_code: {archetype_code!r}")
        return archetype

    candidates = (
        await session.execute(
            select(MilestoneArchetype).where(
                MilestoneArchetype.is_default.is_(True),
                or_(
                    and_(
                        MilestoneArchetype.vertical_id.is_(None),
                        MilestoneArchetype.organisation_id.is_(None),
                    ),
                    and_(
                        MilestoneArchetype.vertical_id == project.vertical_id,
                        MilestoneArchetype.organisation_id.is_(None),
                    ),
                    # Same "within their vertical" scoping as
                    # _resolve_ontology_terms — an org's tenant-level
                    # archetype for one vertical must not leak into their
                    # project in a different one.
                    and_(
                        MilestoneArchetype.vertical_id == project.vertical_id,
                        MilestoneArchetype.organisation_id == project.organisation_id,
                    ),
                ),
            )
        )
    ).scalars().all()
    if not candidates:
        return None
    return max(candidates, key=lambda a: _most_specific(a.organisation_id, a.vertical_id))


async def materialize_archetype(
    session: AsyncSession, project: Project, archetype_code: str | None = None
) -> list[Milestone]:
    """FR-TWN-02/09: seeds a new project's Twin from an archetype template
    (app/twin/models.py's MilestoneArchetype docstring explains why this is
    a template-copy, not a live link) — the one named by `archetype_code` if
    given, otherwise the most specific default for this project's vertical/
    organisation (`_resolve_archetype`). Creates real, project-scoped
    Milestone rows plus real Dependency rows copied edge-for-edge from the
    template's own MilestoneArchetypeDependency rows.

    If `project.event_start` isn't set yet, milestones are still created
    (FR-TWN-01 says the graph must exist) with `planned_at=None` — there's
    nothing for `day_offset` to be relative to until an event date exists.
    A PM setting the event date later is a normal PATCH on each milestone
    (FR-TWN-10), same mechanism as any other override.
    """
    archetype = await _resolve_archetype(session, project, archetype_code)
    if archetype is None:
        return []

    items = (
        await session.execute(
            select(MilestoneArchetypeItem)
            .where(MilestoneArchetypeItem.archetype_id == archetype.id)
            .order_by(MilestoneArchetypeItem.sequence_order)
        )
    ).scalars().all()
    if not items:
        return []

    edges = (
        await session.execute(
            select(MilestoneArchetypeDependency).where(
                MilestoneArchetypeDependency.archetype_id == archetype.id
            )
        )
    ).scalars().all()

    term_by_code = await _resolve_ontology_terms(session, "milestone_type", project)

    created_by_item_id: dict[uuid.UUID, Milestone] = {}
    for item in items:
        term = term_by_code.get(item.type_code)
        if term is None:
            raise LookupError(f"milestone_type ontology term not seeded: {item.type_code!r}")
        planned_at = (
            project.event_start + timedelta(days=item.day_offset)
            if project.event_start is not None
            else None
        )
        milestone = Milestone(
            project_id=project.id,
            type_term_id=term.id,
            name=item.name,
            planned_at=planned_at,
            is_fixed=item.is_fixed,
        )
        session.add(milestone)
        created_by_item_id[item.id] = milestone
    await session.flush()  # need each milestone's id for the dependency rows below

    for edge in edges:
        session.add(
            Dependency(
                project_id=project.id,
                upstream_milestone_id=created_by_item_id[edge.upstream_item_id].id,
                downstream_milestone_id=created_by_item_id[edge.downstream_item_id].id,
                lag_days=edge.lag_days,
            )
        )
    await session.flush()
    return list(created_by_item_id.values())


async def recompute_on_commitment_transition(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    commitment: Commitment,
    to_state: str,
    actor_id: uuid.UUID | None,
) -> dict | None:
    """Prompt 4 step 5 / FR-TWN-04, called from app/api/commitments.py's
    transition_commitment. Synchronous, in-process — no queue, which is what
    trivially clears the 10-second bar.

    Only `delivered` has a deterministic effect on a milestone's date: it's
    evidence the linked deliverable actually happened, so the milestone's
    `actual_at` gets set (first commitment to reach `delivered` against that
    milestone's deliverable wins; a milestone with several deliverables/
    commitments isn't modelled as needing all of them before counting as
    actualised — a real "is this milestone truly done" workflow is more
    scope than this session's job). `at_risk`/`broken` do NOT move any date
    here — deciding *how much* a milestone should slip from a broken
    commitment is forecasting (FR-LCY-02/03), explicitly Foresight's job
    (Prompt 7), not this one's. This function still runs the CPM recompute
    and audits it either way, so Foresight has fresh slack/critical-path
    data to consume once it exists — it just doesn't get to move dates
    itself yet.

    Returns None (recomputing nothing) if the commitment has no deliverable,
    or the deliverable has no milestone.
    """
    if commitment.deliverable_id is None:
        return None
    deliverable = (
        await session.execute(select(Deliverable).where(Deliverable.id == commitment.deliverable_id))
    ).scalar_one_or_none()
    if deliverable is None or deliverable.milestone_id is None:
        return None
    milestone = (
        await session.execute(select(Milestone).where(Milestone.id == deliverable.milestone_id))
    ).scalar_one_or_none()
    if milestone is None:
        return None

    milestone_actualised = False
    if to_state == "delivered" and milestone.actual_at is None:
        milestone.actual_at = datetime.now(dt_timezone.utc)
        milestone_actualised = True
        await session.flush()

    milestones, dependencies = await _load_milestones_and_dependencies(session, project_id)
    nodes, edges = _to_graph(milestones, dependencies)
    result = compute_twin(nodes, edges)
    node = result.nodes.get(milestone.id)

    await record_twin_audit_event(
        session,
        project_id=project_id,
        action="recompute",
        actor_id=actor_id,
        milestone_id=milestone.id,
        detail={
            "triggered_by_commitment_id": str(commitment.id),
            "to_state": to_state,
            "milestone_actual_at_set": milestone_actualised,
            "slack_days": node.slack_days if node else None,
            "is_critical": node.is_critical if node else None,
            "binding_constraint": (
                str(result.binding_constraint) if result.binding_constraint else None
            ),
        },
    )
    return {
        "milestone_id": milestone.id,
        "slack_days": node.slack_days if node else None,
        "binding_constraint": result.binding_constraint,
    }
