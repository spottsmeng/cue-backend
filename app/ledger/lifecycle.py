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

FR-LCY-02/03 (automatic committed -> at_risk / at_risk -> broken) were out of
scope for every session before Foresight (Prompt 7) — they depend on Twin/
Foresight infrastructure that didn't exist yet. Foresight is what finally
makes them real: apply_automatic_transition, below, is what *executes* one
once a detector (app/foresight/silence.py, forecast.py) has *decided* one
should happen. This module's own validator still only answers "is this
transition structurally permitted" — deciding whether one *should* happen
for a given commitment stays Foresight's job, not this module's; see each
detector's own docstring for its trigger logic (FR-LCY-04's delivered-only-
on-evidence gate and FR-LCY-05's PM notification remain separately scoped —
delivered already requires evidence structurally via Commitment's own
evidence requirement, and FR-LCY-05's notification is
app/foresight/notification.py's job, not this module's).
"""

import uuid
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from app.models import Commitment

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


async def apply_automatic_transition(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    commitment: "Commitment",
    to_state: str,
    trigger: str,
    detail: dict | None = None,
) -> None:
    """FR-LCY-02/03: executes an automatic transition a Foresight detector
    has already decided should happen — reuses the exact write path
    app/api/commitments.py's transition_commitment uses for a manual one
    (validate_transition, set state, audit, recompute the Twin), so a
    Foresight-triggered transition is indistinguishable in shape from a
    human-triggered one except for `actor_id=None` (app/ledger/audit.py's
    established "this write has no human behind it" convention,
    app/ledger/extractor.py's own precedent) and the `trigger` string
    recorded in the audit detail, naming *why* (FR-LCY-05 needs this to
    explain the transition to the owning PM).

    Imports app.ledger.audit / app.twin.service locally, not at module
    level — this module is otherwise dependency-free (its own docstring:
    "no ORM/session/network dependency"), and importing at call time avoids
    making every caller of validate_transition/TRANSITIONS (including the
    DB-trigger-comment cross-reference this docstring itself makes) pull in
    the ORM layer just to use the pure state table.

    Does not commit — caller (the detector's scan function) owns the
    transaction boundary.
    """
    from app.ledger.audit import record_audit_event
    from app.parties.service import recompute_vendor_metrics
    from app.twin.service import recompute_on_commitment_transition

    from_state = commitment.state
    validate_transition(from_state, to_state)
    commitment.state = to_state
    await session.flush()  # the DB trigger checks this too — defense in depth, see module docstring

    await record_audit_event(
        session,
        project_id=project_id,
        commitment_id=commitment.id,
        action="state_transition",
        actor_id=None,
        from_state=from_state,
        to_state=to_state,
        detail={"trigger": trigger, **(detail or {})},
    )
    await recompute_on_commitment_transition(
        session, project_id=project_id, commitment=commitment, to_state=to_state, actor_id=None,
    )
    # FR-VRG-03: an automatic transition (silence/forecast-triggered) is
    # still a commitment resolving — same recompute app/api/commitments.py's
    # transition_commitment triggers for a manual one.
    await recompute_vendor_metrics(session, commitment.party_id)
