"""app/twin/graph.py's CPM engine (FR-TWN-03/05/06/11) — pure-function tests
against hand-built graphs, independent of the database, per Prompt 4's own
testing expectation ("CPM correctness on a hand-built graph (independent of
the DB, pure function test)")."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.twin.graph import (
    CycleError,
    DependencyEdge,
    MilestoneNode,
    compute_twin,
    propagate,
)

DAY = timedelta(days=1)
BASE = datetime(2026, 6, 24, tzinfo=timezone.utc)


def _node(offset_days: int, *, is_fixed: bool = False, actual_offset: int | None = None) -> MilestoneNode:
    return MilestoneNode(
        id=uuid.uuid4(),
        planned_at=BASE + offset_days * DAY,
        actual_at=(BASE + actual_offset * DAY) if actual_offset is not None else None,
        is_fixed=is_fixed,
    )


def test_linear_chain_with_exact_lag_is_fully_critical_with_zero_slack():
    a, b, c = _node(-10), _node(-5), _node(0, is_fixed=True)
    edges = [
        DependencyEdge(a.id, b.id, lag_days=5),
        DependencyEdge(b.id, c.id, lag_days=5),
    ]
    result = compute_twin([a, b, c], edges)

    for node in (a, b, c):
        assert result.nodes[node.id].slack_days == 0.0
        assert result.nodes[node.id].is_critical
    assert set(result.critical_path) == {a.id, b.id, c.id}
    # the fixed node is never itself the "binding constraint" (FR-TWN-06) —
    # that's the vendor/workstream pressing against the deadline, not the
    # deadline itself.
    assert result.binding_constraint in (a.id, b.id)


def test_branch_with_slack_is_not_on_the_critical_path():
    # Two independent chains (no shared predecessor, so neither can inherit
    # the other's tightness) converging on the same fixed anchor: `tight`'s
    # own lag exactly matches the gap to `c`, `loose`'s doesn't.
    tight_start, tight = _node(-20), _node(-15)   # (5)-> tight -(15)-> c: 0 slack
    loose_start, loose = _node(-20), _node(-10)   # (5)-> loose -(5)-> c: float
    c = _node(0, is_fixed=True)
    edges = [
        DependencyEdge(tight_start.id, tight.id, lag_days=5),
        DependencyEdge(tight.id, c.id, lag_days=15),
        DependencyEdge(loose_start.id, loose.id, lag_days=5),
        DependencyEdge(loose.id, c.id, lag_days=5),
    ]
    result = compute_twin([tight_start, tight, loose_start, loose, c], edges)

    assert result.nodes[tight.id].slack_days == 0.0
    assert result.nodes[tight.id].is_critical
    assert result.nodes[loose.id].slack_days == 10.0
    assert not result.nodes[loose.id].is_critical
    # tight_start feeds only the tight branch with an exactly-matching lag,
    # so it necessarily shares the same zero slack (correct CPM: a node
    # solely upstream of a critical node is itself critical) — either of the
    # tight branch's two nodes is a legitimate "binding constraint" winner,
    # neither loose-branch node ever is.
    assert result.binding_constraint in (tight_start.id, tight.id)


def test_actual_at_overrides_planned_at_in_forward_pass():
    """A milestone that has genuinely already happened, later than scheduled,
    should push its own earliest date (and anything downstream) to reflect
    reality, not the stale plan."""
    a = MilestoneNode(id=uuid.uuid4(), planned_at=BASE - 10 * DAY, actual_at=BASE - 6 * DAY, is_fixed=False)
    b = _node(-2)
    edges = [DependencyEdge(a.id, b.id, lag_days=2)]

    result = compute_twin([a, b], edges)
    assert result.nodes[a.id].earliest == BASE - 6 * DAY
    # b's earliest is now forced later than its own -2 day plan: -6 + 2 = -4,
    # still earlier than planned (-2), so b keeps its own slack — but the
    # forward pass must reflect the actual, not the plan.
    assert result.nodes[b.id].earliest == BASE - 4 * DAY


def test_cyclic_graph_raises_cycle_error():
    a, b = _node(-5), _node(0)
    edges = [DependencyEdge(a.id, b.id, lag_days=1), DependencyEdge(b.id, a.id, lag_days=1)]
    with pytest.raises(CycleError):
        compute_twin([a, b], edges)


def test_propagate_a_delay_consumes_slack_downstream():
    tight_start, tight = _node(-20), _node(-15)
    loose_start, loose = _node(-20), _node(-10)
    c = _node(0, is_fixed=True)
    edges = [
        DependencyEdge(tight_start.id, tight.id, lag_days=5),
        DependencyEdge(tight.id, c.id, lag_days=15),
        DependencyEdge(loose_start.id, loose.id, lag_days=5),
        DependencyEdge(loose.id, c.id, lag_days=5),
    ]
    nodes = [tight_start, tight, loose_start, loose, c]
    baseline = compute_twin(nodes, edges)
    assert baseline.nodes[loose.id].slack_days == 10.0  # sanity: matches the branch test above

    # Shift `loose` 4 days later than its own graph-derived earliest date
    # (not its `planned_at` — slack is measured against the feasible window,
    # same as the assertion just above) — 4 of its 10 days of float are
    # consumed, and it still isn't tight enough to become binding.
    new_date = baseline.nodes[loose.id].earliest + 4 * DAY
    result = propagate(nodes, edges, loose.id, new_date)
    by_id = {a.milestone_id: a for a in result.affected}

    assert by_id[loose.id].consumed_slack_days == pytest.approx(4.0)
    assert not by_id[loose.id].propagation_stopped
    assert tight.id not in by_id  # the tight branch shares no edge with `loose`, unaffected
    # still the tighter branch (see the tie-break note in the test above)
    assert result.binding_constraint_after in (tight_start.id, tight.id)


def test_propagate_stops_at_a_fixed_node_and_does_not_move_it():
    a, b = _node(-10), _node(-5)
    c = _node(0, is_fixed=True)  # e.g. "doors" — immovable
    d_downstream_of_fixed = _node(5)  # something scheduled after doors, e.g. "strike"
    edges = [
        DependencyEdge(a.id, b.id, lag_days=5),
        DependencyEdge(b.id, c.id, lag_days=5),
        DependencyEdge(c.id, d_downstream_of_fixed.id, lag_days=5),
    ]
    # Push `a` 3 days later — by the time this reaches the fixed node, it
    # would overrun it by 3 days.
    result = propagate([a, b, c, d_downstream_of_fixed], edges, a.id, BASE - 10 * DAY + 3 * DAY)
    by_id = {aff.milestone_id: aff for aff in result.affected}

    assert c.id in by_id
    assert by_id[c.id].propagation_stopped is True
    assert by_id[c.id].new_earliest == by_id[c.id].previous_earliest  # never moves
    assert by_id[c.id].consumed_slack_days == pytest.approx(3.0)  # the overrun pressure, reported anyway
    # Nothing beyond the fixed node was touched — this is the actual
    # "genuinely stops propagation" assertion, not just the flag on c.
    assert d_downstream_of_fixed.id not in by_id


def test_binding_constraint_is_none_when_no_slack_is_computable():
    """A genuinely unscheduled milestone (no planned_at, no predecessors, no
    successors) has no feasible window to measure at all — no crash, just no
    answer, rather than a false 0.0."""
    a = MilestoneNode(id=uuid.uuid4(), planned_at=None, actual_at=None, is_fixed=False)
    result = compute_twin([a], [])
    assert result.nodes[a.id].slack_days is None
    assert result.binding_constraint is None
    assert result.critical_path == []


def test_a_scheduled_lone_node_is_trivially_on_schedule():
    """A single node that DOES have a planned_at, with nothing upstream or
    downstream, is its own earliest and latest — slack 0, not None; there is
    simply nothing else in the graph to be off-schedule relative to."""
    a = _node(0)
    result = compute_twin([a], [])
    assert result.nodes[a.id].slack_days == 0.0
    assert result.binding_constraint == a.id
