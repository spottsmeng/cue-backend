"""FR-NRM-03 (PRD §6.2): "Resolve channel identities (phone number, WeChat
ID, UPN, email) to a single `party` record, with confidence and manual
override." Real implementation, for anything arriving through a real
ChannelAdapter (item 3's queue pipeline, app/capture/pipeline.py).

Deliberately not a rewrite of app/ledger/extractor.py's `_get_or_create_party`
— that function's exact-string-match against `Party.display_name` stays
exactly as-is, since it is what every existing fixture-based test
(scripts/extract_fixtures.py, cue-eval's own fixture-adapter path) already
depends on, and Prompt 11's own instruction is explicit that changing it
risks breaking that suite for no benefit real capture needs. This module is
a strict superset for the real-capture path only: it resolves against the
`channel_identities` table first (schema already existed since the
identity/RBAC session, unused until now), which `_get_or_create_party` never
touches at all.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ChannelIdentity, Party

# FR-NRM-03: "with confidence and manual override." A manually_verified=True
# ChannelIdentity (set via set_manual_identity_override below) always
# resolves at confidence 1.0 regardless of how it originally got created — a
# human correction is ground truth, not a heuristic to be re-scored.
_EXACT_MATCH_CONFIDENCE = 1.0
# A heuristic link to an *existing* Party by case-insensitive display-name
# match, with no prior ChannelIdentity to ground it — plausible, not
# certain (the same display name can legitimately belong to two different
# people at two different vendor orgs), so this deliberately lands low
# enough to be an obvious "needs a human look" signal, the same posture
# FR-LED-07 already gives price/date/approval fields.
_DISPLAY_NAME_MATCH_CONFIDENCE = 0.6
# A brand-new external_id with no plausible existing Party to link to mints
# a brand-new Party — an unambiguous 1:1 mapping, so full confidence; there
# is nothing yet for this resolution to be uncertain relative to.
_NEW_PARTY_CONFIDENCE = 1.0


@dataclass
class IdentityResolution:
    party_id: uuid.UUID
    confidence: float
    manually_verified: bool
    created_new_identity: bool
    created_new_party: bool
    # The resolved Party ORM object, populated only when
    # created_new_identity is True (steps 2/3 below already have it in hand
    # from linking/minting it — attaching it here costs no extra query).
    # None on the common fast path (step 1, an identity that already
    # existed) — deliberately not fetched there, since that's the hot path
    # for every repeat sender and nothing currently calling resolve_identity
    # needs the Party object for an already-resolved identity. A caller that
    # ever does can fetch it by party_id itself; that's a second query only
    # on a path that doesn't run on every message.
    party: Party | None = None


async def resolve_identity(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    channel_type: str,
    external_id: str,
    display_name: str | None = None,
    default_party_type: str = "person",
) -> IdentityResolution:
    """Resolves (channel_type, external_id) to a Party, in three steps:

    1. An existing `channel_identities` row for this exact (channel_type,
       external_id) always wins outright, at whatever confidence/manual-
       verification it already carries — once an identity is resolved, it
       isn't re-scored on every subsequent message from the same sender.
    2. No existing identity, but `display_name` case-insensitively matches
       an existing Party in this organisation: link at
       _DISPLAY_NAME_MATCH_CONFIDENCE.
    3. Neither: mint a brand-new Party at _NEW_PARTY_CONFIDENCE.

    Every path persists a `channel_identities` row (steps 2 and 3 create
    one; step 1 reads one that already exists) so the *next* message from
    this same external_id always resolves via step 1, not a re-run of the
    heuristic in step 2.
    """
    existing_identity = (
        await session.execute(
            select(ChannelIdentity).where(
                ChannelIdentity.channel_type == channel_type,
                ChannelIdentity.external_id == external_id,
            )
        )
    ).scalar_one_or_none()
    if existing_identity is not None:
        return IdentityResolution(
            party_id=existing_identity.party_id,
            confidence=existing_identity.confidence,
            manually_verified=existing_identity.manually_verified,
            created_new_identity=False,
            created_new_party=False,
            party=None,
        )

    party: Party | None = None
    confidence = _NEW_PARTY_CONFIDENCE
    if display_name:
        party = (
            await session.execute(
                select(Party).where(
                    Party.organisation_id == organisation_id,
                    func.lower(Party.display_name) == display_name.strip().lower(),
                )
            )
        ).scalar_one_or_none()
        if party is not None:
            confidence = _DISPLAY_NAME_MATCH_CONFIDENCE

    created_new_party = party is None
    if party is None:
        party = Party(
            organisation_id=organisation_id,
            display_name=display_name or external_id,
            type=default_party_type,
        )
        session.add(party)
        await session.flush()

    identity = ChannelIdentity(
        party_id=party.id,
        channel_type=channel_type,
        external_id=external_id,
        confidence=confidence,
        manually_verified=False,
    )
    session.add(identity)
    await session.flush()

    return IdentityResolution(
        party_id=party.id,
        confidence=confidence,
        manually_verified=False,
        created_new_identity=True,
        created_new_party=created_new_party,
        party=party,
    )


async def set_manual_identity_override(
    session: AsyncSession,
    *,
    channel_type: str,
    external_id: str,
    party_id: uuid.UUID,
) -> ChannelIdentity:
    """FR-NRM-03's "manual override" — an Administrator correcting a
    resolution (a wrong auto-match, or a display-name heuristic that turned
    out to be the wrong person). Upserts by the (channel_type, external_id)
    unique constraint, same idempotent-by-natural-key shape
    app/api/consent.py's consent_action_request already uses for
    ConsentRecord. Always lands at confidence 1.0, manually_verified=True —
    a human correction is definitionally certain, and (per this module's own
    resolve_identity) never gets re-scored by the heuristic afterward."""
    existing = (
        await session.execute(
            select(ChannelIdentity).where(
                ChannelIdentity.channel_type == channel_type,
                ChannelIdentity.external_id == external_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.party_id = party_id
        existing.confidence = _EXACT_MATCH_CONFIDENCE
        existing.manually_verified = True
        await session.flush()
        return existing

    identity = ChannelIdentity(
        party_id=party_id,
        channel_type=channel_type,
        external_id=external_id,
        confidence=_EXACT_MATCH_CONFIDENCE,
        manually_verified=True,
    )
    session.add(identity)
    await session.flush()
    return identity
