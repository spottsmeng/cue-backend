"""FR-TWN-03/05/06/11: critical-path-method computation over a project's
milestone/dependency graph, and hypothetical date-shift propagation.

Pure Python, no ORM/session/network dependency by design (Prompt 4's own
instruction: "no new infrastructure dependency needed at this scale") — every
function here takes plain dataclasses and returns plain dataclasses, so it is
testable as ordinary unit tests against a hand-built graph, independent of
Postgres. app/twin/service.py is the only caller that knows how to load a
MilestoneNode/DependencyEdge list from the database.

Model, briefly: each milestone is a zero-duration *event* with its own
`planned_at` (the schedule/target date — from the seeded archetype or a PM
override) and optionally `actual_at` (ground truth, once it has really
happened). Each dependency edge says "downstream must be at least `lag_days`
after upstream." This is CPM with dated events rather than durations:

- `earliest[n]` (forward pass): the earliest this node could plausibly land,
  purely as a function of its predecessors — `actual_at` if it has already
  happened, else the max of every predecessor's earliest + lag, else (a
  source node with no predecessors) its own `planned_at`. This deliberately
  ignores a non-source node's own `planned_at` once it has predecessors, the
  same way classical CPM's Early Start is graph-derived, not read off an
  externally given target — otherwise slack could never be anything but
  zero even where real float exists.
- `latest[n]` (backward pass): the latest this node can land without forcing
  a fixed node past its own `planned_at` — a fixed node's `latest` IS its
  `planned_at` (FR-TWN-11: "cannot be propagated past"); a node with no
  successors has nothing downstream constraining it, so its `latest` is just
  its own `planned_at`.
- `slack[n] = latest[n] - earliest[n]`, in days. Zero (or the graph's
  minimum, which can be negative if the network is already behind) marks the
  critical path.
"""

from __future__ import annotations

import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta


class CycleError(Exception):
    """The dependency graph is not a DAG (FR-TWN-01 requires one) — raised
    rather than silently producing a nonsensical partial order."""


@dataclass(frozen=True)
class MilestoneNode:
    id: uuid.UUID
    planned_at: datetime | None
    actual_at: datetime | None
    is_fixed: bool


@dataclass(frozen=True)
class DependencyEdge:
    upstream_id: uuid.UUID
    downstream_id: uuid.UUID
    lag_days: int


@dataclass(frozen=True)
class NodeResult:
    milestone_id: uuid.UUID
    earliest: datetime | None
    latest: datetime | None
    slack_days: float | None
    is_critical: bool


@dataclass(frozen=True)
class TwinResult:
    nodes: dict[uuid.UUID, NodeResult]
    critical_path: list[uuid.UUID]
    binding_constraint: uuid.UUID | None


@dataclass(frozen=True)
class AffectedNode:
    milestone_id: uuid.UUID
    previous_earliest: datetime | None
    new_earliest: datetime | None
    consumed_slack_days: float | None
    propagation_stopped: bool = field(
        default=False,
        metadata={"doc": "FR-TWN-11: this node is_fixed and absorbed the shift; nothing beyond it moved"},
    )


@dataclass(frozen=True)
class PropagationResult:
    shifted_milestone_id: uuid.UUID
    new_date: datetime
    affected: list[AffectedNode]
    binding_constraint_after: uuid.UUID | None


def _topological_order(
    node_ids: list[uuid.UUID], edges: list[DependencyEdge]
) -> tuple[list[uuid.UUID], dict[uuid.UUID, list[DependencyEdge]], dict[uuid.UUID, list[DependencyEdge]]]:
    """Kahn's algorithm. Returns (order, incoming-edges-by-node, outgoing-edges-by-node)."""
    incoming: dict[uuid.UUID, list[DependencyEdge]] = {n: [] for n in node_ids}
    outgoing: dict[uuid.UUID, list[DependencyEdge]] = {n: [] for n in node_ids}
    indegree: dict[uuid.UUID, int] = {n: 0 for n in node_ids}
    for e in edges:
        outgoing[e.upstream_id].append(e)
        incoming[e.downstream_id].append(e)
        indegree[e.downstream_id] += 1

    queue = deque(n for n in node_ids if indegree[n] == 0)
    order: list[uuid.UUID] = []
    remaining = dict(indegree)
    while queue:
        n = queue.popleft()
        order.append(n)
        for e in outgoing[n]:
            remaining[e.downstream_id] -= 1
            if remaining[e.downstream_id] == 0:
                queue.append(e.downstream_id)

    if len(order) != len(node_ids):
        raise CycleError("milestone dependency graph is not acyclic (FR-TWN-01)")
    return order, incoming, outgoing


def _max_date(candidates: list[datetime | None]) -> datetime | None:
    known = [c for c in candidates if c is not None]
    return max(known) if known else None


def _min_date(candidates: list[datetime | None]) -> datetime | None:
    known = [c for c in candidates if c is not None]
    return min(known) if known else None


def _forward_pass(
    order: list[uuid.UUID],
    nodes: dict[uuid.UUID, MilestoneNode],
    incoming: dict[uuid.UUID, list[DependencyEdge]],
) -> dict[uuid.UUID, datetime | None]:
    earliest: dict[uuid.UUID, datetime | None] = {}
    for n in order:
        node = nodes[n]
        preds = incoming[n]
        if node.is_fixed:
            earliest[n] = node.planned_at
        elif node.actual_at is not None:
            earliest[n] = node.actual_at
        elif preds:
            earliest[n] = _max_date(
                [
                    earliest[e.upstream_id] + timedelta(days=e.lag_days)
                    if earliest[e.upstream_id] is not None
                    else None
                    for e in preds
                ]
            )
        else:
            earliest[n] = node.planned_at
    return earliest


def _backward_pass(
    order: list[uuid.UUID],
    nodes: dict[uuid.UUID, MilestoneNode],
    outgoing: dict[uuid.UUID, list[DependencyEdge]],
) -> dict[uuid.UUID, datetime | None]:
    latest: dict[uuid.UUID, datetime | None] = {}
    for n in reversed(order):
        node = nodes[n]
        succs = outgoing[n]
        if node.is_fixed:
            latest[n] = node.planned_at
        elif not succs:
            latest[n] = node.planned_at
        else:
            latest[n] = _min_date(
                [
                    latest[e.downstream_id] - timedelta(days=e.lag_days)
                    if latest[e.downstream_id] is not None
                    else None
                    for e in succs
                ]
            )
    return latest


def _slack_days(latest: datetime | None, earliest: datetime | None) -> float | None:
    if latest is None or earliest is None:
        return None
    return (latest - earliest).total_seconds() / 86400.0


def _consumed_slack(
    latest_date: datetime | None, previous_earliest: datetime | None, new_earliest: datetime | None
) -> float | None:
    """How much of a node's buffer a hypothetical shift ate — FR-TWN-05's own
    wording. `latest_date` never changes under a hypothetical forward shift
    (see propagate()'s docstring), so only the earliest side moves."""
    prev_slack = _slack_days(latest_date, previous_earliest)
    new_slack = _slack_days(latest_date, new_earliest)
    if prev_slack is None or new_slack is None:
        return None
    return prev_slack - new_slack


def _binding_constraint(
    nodes: dict[uuid.UUID, MilestoneNode], slack: dict[uuid.UUID, float | None]
) -> uuid.UUID | None:
    """FR-TWN-06: the vendor/workstream node with the least slack — a fixed
    node (doors, venue hand-back) is the deadline, never the "constraint"
    that's binding against it, so it's excluded from candidacy. Ties broken
    by id for determinism."""
    candidates = [
        (slack_value, str(node_id))
        for node_id, node in nodes.items()
        if not node.is_fixed and (slack_value := slack.get(node_id)) is not None
    ]
    if not candidates:
        return None
    _, winner_str = min(candidates, key=lambda c: (c[0], c[1]))
    return uuid.UUID(winner_str)


def compute_twin(
    nodes: list[MilestoneNode], edges: list[DependencyEdge]
) -> TwinResult:
    """FR-TWN-03: critical path, per-node slack, current binding constraint."""
    node_map = {n.id: n for n in nodes}
    order, incoming, outgoing = _topological_order(list(node_map.keys()), edges)

    earliest = _forward_pass(order, node_map, incoming)
    latest = _backward_pass(order, node_map, outgoing)
    slack = {n: _slack_days(latest[n], earliest[n]) for n in order}

    known_slacks = [s for s in slack.values() if s is not None]
    min_slack = min(known_slacks) if known_slacks else None
    critical_path = [n for n in order if slack[n] is not None and slack[n] == min_slack] if known_slacks else []

    results = {
        n: NodeResult(
            milestone_id=n,
            earliest=earliest[n],
            latest=latest[n],
            slack_days=slack[n],
            is_critical=n in critical_path,
        )
        for n in order
    }
    return TwinResult(
        nodes=results,
        critical_path=critical_path,
        binding_constraint=_binding_constraint(node_map, slack),
    )


def propagate(
    nodes: list[MilestoneNode],
    edges: list[DependencyEdge],
    milestone_id: uuid.UUID,
    new_date: datetime,
) -> PropagationResult:
    """FR-TWN-05/06/07: simulate `milestone_id` landing on `new_date` and
    report the downstream impact — read-only, mutates nothing (the caller
    never persists this). Doubles as FR-TWN-07's "candidate recovery
    options" surface: a PM proposes a candidate (try shifting X, or
    expediting Y) and this reports its vendor/downstream impact for them to
    weigh, rather than CUE inventing candidates itself (would-be FR-TWN-08
    territory — a search/optimisation heuristic with nothing real to learn
    from yet). Calling this multiple times with different milestone_id/
    new_date pairs, one call per candidate, is how a PM compares options;
    there is no separate endpoint or function for that.
    """
    node_map = {n.id: n for n in nodes}
    if milestone_id not in node_map:
        raise KeyError(f"no such milestone in this graph: {milestone_id}")

    order, incoming, outgoing = _topological_order(list(node_map.keys()), edges)
    baseline = compute_twin(nodes, edges)
    baseline_earliest = {n: baseline.nodes[n].earliest for n in order}
    baseline_slack = {n: baseline.nodes[n].slack_days for n in order}
    latest = _backward_pass(order, node_map, outgoing)  # unchanged by a hypothetical forward shift

    order_index = {n: i for i, n in enumerate(order)}
    downstream = sorted(_downstream_closure(milestone_id, outgoing), key=lambda n: order_index[n])

    shifted_earliest = dict(baseline_earliest)
    shifted_earliest[milestone_id] = new_date

    affected: list[AffectedNode] = [
        AffectedNode(
            milestone_id=milestone_id,
            previous_earliest=baseline_earliest[milestone_id],
            new_earliest=new_date,
            consumed_slack_days=_consumed_slack(
                latest[milestone_id], baseline_earliest[milestone_id], new_date
            ),
        )
    ]

    stopped: set[uuid.UUID] = set()
    for n in downstream:
        # Predecessors through an already-stopped fixed node don't carry the
        # shift any further (FR-TWN-11) — everyone else contributes either
        # their shifted date or, if untouched by this shift, their baseline
        # one (shifted_earliest was seeded from baseline for every node).
        candidate = _max_date(
            [
                shifted_earliest[e.upstream_id] + timedelta(days=e.lag_days)
                if shifted_earliest.get(e.upstream_id) is not None
                else None
                for e in incoming[n]
                if e.upstream_id not in stopped
            ]
        )
        if candidate is None or candidate == baseline_earliest[n]:
            continue  # unaffected by this hypothetical

        node = node_map[n]
        if node.is_fixed:
            affected.append(
                AffectedNode(
                    milestone_id=n,
                    previous_earliest=baseline_earliest[n],
                    new_earliest=baseline_earliest[n],  # FR-TWN-11: does not move
                    consumed_slack_days=_consumed_slack(latest[n], baseline_earliest[n], candidate),
                    propagation_stopped=True,
                )
            )
            stopped.add(n)
            continue

        shifted_earliest[n] = candidate
        affected.append(
            AffectedNode(
                milestone_id=n,
                previous_earliest=baseline_earliest[n],
                new_earliest=candidate,
                consumed_slack_days=_consumed_slack(latest[n], baseline_earliest[n], candidate),
            )
        )

    effective_slack = dict(baseline_slack)
    for a in affected:
        new_slack = _slack_days(latest[a.milestone_id], a.new_earliest)
        effective_slack[a.milestone_id] = new_slack
    binding_constraint_after = _binding_constraint(node_map, effective_slack)

    return PropagationResult(
        shifted_milestone_id=milestone_id,
        new_date=new_date,
        affected=affected,
        binding_constraint_after=binding_constraint_after,
    )


def _downstream_closure(
    start_id: uuid.UUID, outgoing: dict[uuid.UUID, list[DependencyEdge]]
) -> set[uuid.UUID]:
    reachable: set[uuid.UUID] = set()
    stack = [start_id]
    while stack:
        n = stack.pop()
        for e in outgoing.get(n, []):
            if e.downstream_id not in reachable:
                reachable.add(e.downstream_id)
                stack.append(e.downstream_id)
    return reachable
