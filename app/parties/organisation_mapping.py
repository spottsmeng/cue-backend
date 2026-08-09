"""FR-NRM-04 (PRD §6.2): "Maintain party-to-organisation mapping (person ->
vendor company) with effective-dating." Real implementation over
app/capture/models.py's PartyOrganisationMapping — see that class's own
docstring for why this is a history table, not a mutable field on Party.

Lives in app/parties/, not app/capture/, even though FR-NRM-04 is numbered
alongside FR-NRM-03 in the PRD — this is squarely party-domain logic (who a
person currently represents), the same reasoning app/parties/reliability.py
and app/parties/compute.py already sit in this package rather than in
whichever session's prompt happened to name them (Vendor Reliability Graph,
Prompt 10). The table itself stays in app/capture/models.py only because
that migration (Prompt 11 item 1) is what created it — models and the
service that owns their invariants don't have to share a package, and
CUE's own established precedent (Evidence lives in app/models/ledger.py but
is written to by app/documents/, app/foresight/ and app/capture/ alike)
already establishes that split.
"""

import uuid
from datetime import datetime, timezone as dt_timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.capture.models import PartyOrganisationMapping
from app.models import Party


class OrganisationMappingError(ValueError):
    """Raised when the party types don't match FR-NRM-04's own shape
    (person -> vendor company) — checked in code, not a DB constraint,
    since Party.type is shared with every other party kind and a CHECK
    here would need a cross-table lookup CHECK can't express."""


async def _require_party_type(session: AsyncSession, party_id: uuid.UUID, expected_type: str) -> Party:
    party = (await session.execute(select(Party).where(Party.id == party_id))).scalar_one_or_none()
    if party is None:
        raise OrganisationMappingError(f"no such party: {party_id}")
    if party.type != expected_type:
        raise OrganisationMappingError(
            f"party {party_id} has type {party.type!r}, expected {expected_type!r}"
        )
    return party


async def set_current_organisation(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    person_party_id: uuid.UUID,
    organisation_party_id: uuid.UUID,
    role_title: str | None = None,
    effective_from: datetime | None = None,
) -> PartyOrganisationMapping:
    """FR-NRM-04's write path — a person moving to (or first being linked
    to) a vendor company. Closes whatever mapping was previously open for
    this person (`effective_to` set to the new mapping's own
    `effective_from`, never left dangling open twice for the same person)
    before opening the new one, so `get_current_organisation` below always
    has exactly one open row per person to find, not zero or two.

    Re-affirming the *same* organisation_party_id the person is already
    mapped to is a no-op (returns the existing open row unchanged) rather
    than closing and reopening an identical mapping, which would fragment
    one continuous tenure into two history rows for no real-world change.
    """
    await _require_party_type(session, person_party_id, "person")
    await _require_party_type(session, organisation_party_id, "vendor_org")

    effective_from = effective_from or datetime.now(dt_timezone.utc)

    current = await get_current_organisation(session, person_party_id, at=effective_from)
    if current is not None and current.organisation_party_id == organisation_party_id:
        return current

    if current is not None:
        current.effective_to = effective_from
        await session.flush()

    mapping = PartyOrganisationMapping(
        organisation_id=organisation_id,
        person_party_id=person_party_id,
        organisation_party_id=organisation_party_id,
        role_title=role_title,
        effective_from=effective_from,
        effective_to=None,
    )
    session.add(mapping)
    await session.flush()
    return mapping


async def get_current_organisation(
    session: AsyncSession, person_party_id: uuid.UUID, *, at: datetime | None = None
) -> PartyOrganisationMapping | None:
    """Which vendor company this person currently (or, with `at`, at some
    point in the project's history) represents — `at` defaults to now, so a
    caller asking "who does this contact work for right now" doesn't need
    to know effective-dating exists at all."""
    at = at or datetime.now(dt_timezone.utc)
    stmt = (
        select(PartyOrganisationMapping)
        .where(
            PartyOrganisationMapping.person_party_id == person_party_id,
            PartyOrganisationMapping.effective_from <= at,
        )
        .order_by(PartyOrganisationMapping.effective_from.desc())
    )
    for mapping in (await session.execute(stmt)).scalars().all():
        if mapping.effective_to is None or mapping.effective_to > at:
            return mapping
    return None


async def get_organisation_history(
    session: AsyncSession, person_party_id: uuid.UUID
) -> list[PartyOrganisationMapping]:
    """The full FR-NRM-04 history for a person, oldest first — e.g. for a
    UI timeline, or for Foresight to reason about "which vendor was this
    contact with when they made commitment X"."""
    stmt = (
        select(PartyOrganisationMapping)
        .where(PartyOrganisationMapping.person_party_id == person_party_id)
        .order_by(PartyOrganisationMapping.effective_from.asc())
    )
    return list((await session.execute(stmt)).scalars().all())
