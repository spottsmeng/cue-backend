"""FR-LCY-01 (PRD §6.5): the commitment lifecycle state machine.

Enforced in two places, deliberately:

1. Here — an explicit transition table, checked before any write. This is the
   primary path: it runs before a DB round-trip, gives a caller a typed
   `InvalidTransition` it can turn into an HTTP 409 without parsing a
   Postgres error string, and is the only layer that can carry a human-
   readable reason.
2. A `BEFORE UPDATE` trigger on `commitments` (the migration that added this
   table), checked again in the database. Not deferred like the evidence
   trigger — a transition's validity depends only on OLD.state and NEW.state
   on the same row, no child-table cardinality to wait for, so it can run
   immediately per-statement.

Both layers exist for the same reason CLAUDE.md gives for evidence spans:
application code is not trusted alone, because it is not the only writer —
a script, a future service, or a migration could update `commitments.state`
directly. Unlike evidence (which genuinely cannot be expressed as a
column-level CHECK), transition validity *can* be expressed as a trigger, so
there is no reason to settle for one layer here. The two tables below must be
kept in sync by hand; the migration's SQL carries a comment pointing back to
this file, and vice versa.

Out of scope this session (PRD §6.5's FR-LCY-02/03/04/05): automatic
transitions from silence/upstream-slip/forecast-breach, the delivered-only-
on-evidence gate, and PM notification — those depend on Twin/Foresight
infrastructure that doesn't exist yet. This module only validates that a
requested transition is *structurally* permitted; it does not decide whether
one *should* happen.
"""

# proposed --> proposed ("renegotiate terms", per the PRD's state diagram) is
# a genuine self-loop, not an omission — new terms on an unaccepted proposal
# don't yet warrant a new state.
TRANSITIONS: dict[str, frozenset[str]] = {
    "proposed": frozenset({"committed", "withdrawn", "proposed"}),
    "committed": frozenset({"at_risk", "delivered", "renegotiated", "withdrawn"}),
    "at_risk": frozenset({"delivered", "renegotiated", "broken"}),
    "renegotiated": frozenset({"committed", "withdrawn"}),
    "broken": frozenset({"renegotiated"}),
    "delivered": frozenset(),
    "withdrawn": frozenset(),
}


class InvalidTransition(Exception):
    """Raised when a requested state change is not in TRANSITIONS. Never
    persisted, never silently coerced — the caller decides how to surface it
    (the REST layer turns this into a 409)."""


def validate_transition(from_state: str, to_state: str) -> None:
    if to_state not in TRANSITIONS.get(from_state, frozenset()):
        raise InvalidTransition(
            f"{from_state!r} -> {to_state!r} is not a permitted commitment transition (PRD §6.5)"
        )
