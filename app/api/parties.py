import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    require_org_administrator,
    require_org_finance,
    require_org_finance_or_administrator,
)
from app.api.schemas import PartyOrganisationMappingOut, PartyOrganisationMappingSet, PartyOut, PartyTypeLiteral
from app.core.db import get_session
from app.identity.models import User
from app.models import OntologyTerm, Party
from app.parties.organisation_mapping import (
    OrganisationMappingError,
    get_current_organisation,
    get_organisation_history,
    set_current_organisation,
)
from app.parties.reliability import get_reliability_history, get_reliability_metrics
from app.parties.schema import (
    VendorMetricNameLiteral,
    VendorMetricOut,
    VendorReliabilityHistoryOut,
    VendorReliabilityOut,
)

# CUE-PRD.md §11.2: `/parties/{id}/reliability` — metrics · history. Gated
# on FINANCE_ROLES (Procurement-tier access) org-wide, not project-scoped —
# see app/api/deps.py's require_org_finance for why this can't reuse
# require_project_role. FR-VRG-07 ("never expose a vendor's metrics to
# another vendor") is satisfied by construction: this is the *only* route
# this module registers, there is no vendor-authenticatable identity
# anywhere in this codebase (P1 has no vendor-facing surface at all — every
# bearer token belongs to an internal Pico user), and it is gated exactly
# like every other internal-only resource. See
# tests/test_parties_reliability_api.py for the explicit proof.
router = APIRouter(prefix="/parties/{party_id}/reliability", tags=["parties"])


async def _get_party(session: AsyncSession, organisation_id: uuid.UUID, party_id: uuid.UUID) -> Party:
    """`parties` has no RLS policy of its own (a pre-existing gap from the
    Ledger session — app/api/commitments.py's `_require_party` docstring
    already documents this), so this explicit organisation_id filter is
    what actually stands between a caller and reading another tenant's
    party by guessing its id. Same reasoning, same fix, applied here."""
    party = (
        await session.execute(
            select(Party).where(Party.id == party_id, Party.organisation_id == organisation_id)
        )
    ).scalar_one_or_none()
    if party is None:
        raise HTTPException(status_code=404, detail="party not found")
    return party


def _metric_out(snapshot) -> VendorMetricOut:
    return VendorMetricOut(
        metric=snapshot.metric,
        value=snapshot.value,
        available=snapshot.available,
        unavailable_reason=snapshot.unavailable_reason,
        sample_size=snapshot.sample_size,
        computed_at=snapshot.computed_at,
        segment_vendor_category=snapshot.segment_vendor_category,
        segment_city=snapshot.segment_city,
        segment_event_archetype=snapshot.segment_event_archetype,
    )


@router.get("", response_model=VendorReliabilityOut)
async def read_vendor_reliability(
    party_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(require_org_finance)],
    event_archetype: str | None = None,
    vendor_category: str | None = None,
    city: str | None = None,
) -> VendorReliabilityOut:
    """FR-VRG-01/02: the current snapshot of every metric computed for this
    vendor so far, in one segment. `event_archetype`/`vendor_category`/
    `city` are FR-VRG-02's segmentation filters — see
    app/parties/reliability.py's own docstring for exactly what each one
    means for a single-vendor endpoint like this."""
    party = await _get_party(session, user.organisation_id, party_id)
    snapshots = await get_reliability_metrics(
        session,
        party.id,
        event_archetype=event_archetype,
        vendor_category=vendor_category,
        city=city,
    )
    return VendorReliabilityOut(
        party_id=party.id,
        event_archetype=event_archetype,
        vendor_category=vendor_category,
        city=city,
        metrics=[_metric_out(s) for s in snapshots.values()],
    )


@router.get("/history", response_model=VendorReliabilityHistoryOut)
async def read_vendor_reliability_history(
    party_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(require_org_finance)],
    metric: VendorMetricNameLiteral | None = None,
    event_archetype: str | None = None,
) -> VendorReliabilityHistoryOut:
    """FR-VRG-03's "update continuously" made visible as a trend: every
    snapshot ever recomputed for this vendor in this segment, oldest
    first."""
    party = await _get_party(session, user.organisation_id, party_id)
    snapshots = await get_reliability_history(
        session, party.id, metric=metric, event_archetype=event_archetype
    )
    return VendorReliabilityHistoryOut(
        party_id=party.id,
        event_archetype=event_archetype,
        metric=metric,
        history=[_metric_out(s) for s in snapshots],
    )


# GET /parties — the vendor/contact directory the reliability endpoint
# above never had: /parties/{id}/reliability needs a party_id the caller
# already knows, and until this router existed there was no route in this
# codebase that could tell a caller which party_ids exist at all. Same
# require_org_finance gate as reliability (Procurement-tier, org-wide, not
# project-scoped — Party has no project_id of its own, per its own
# docstring); same explicit organisation_id filter as _get_party above,
# for the same reason (`parties` has no RLS policy of its own).
list_router = APIRouter(prefix="/parties", tags=["parties"])


@list_router.get("", response_model=list[PartyOut])
async def list_parties(
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(require_org_finance)],
    type: PartyTypeLiteral | None = None,
    city: str | None = None,
    vendor_category: str | None = None,
) -> list[Party]:
    """`vendor_category` filters by ontology_terms.code via a direct join on
    Party.vendor_category_term_id — a party's own category was already
    resolved to one specific tier's row at assignment time (FR-VRG-02), so
    matching that row's code directly is unambiguous; this does not need
    the three-tier *resolution* app/twin/service.py's list_ontology_terms
    performs when validating a caller-supplied code, only a lookup against
    a code a caller is filtering by."""
    stmt = select(Party).where(Party.organisation_id == user.organisation_id)
    if type is not None:
        stmt = stmt.where(Party.type == type)
    if city is not None:
        stmt = stmt.where(Party.city == city)
    if vendor_category is not None:
        stmt = stmt.join(OntologyTerm, Party.vendor_category_term_id == OntologyTerm.id).where(
            OntologyTerm.code == vendor_category
        )
    stmt = stmt.order_by(Party.display_name)
    return list((await session.execute(stmt)).scalars().all())


# Frontend-enablement addition (Prompt F6's own gap-audit check, frontend/
# CLAUDE.md's "Class A" shape — though this one is closer to a missing
# single-resource read than a missing label resolver): a vendor detail page
# needs this party's own display_name/type/city to render at all (a page
# header, and to decide whether FR-NRM-04's organisation-mapping section is
# even applicable — that's person-only), and until this route existed the
# only way to learn them was to re-fetch the entire org-wide list above and
# find the one row client-side. Same require_org_finance gate, same
# explicit organisation_id filter (via _get_party) as every other route in
# this module — no new access tier introduced.
@list_router.get("/{party_id}", response_model=PartyOut)
async def read_party(
    party_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(require_org_finance)],
) -> Party:
    return await _get_party(session, user.organisation_id, party_id)


# FR-NRM-04: a person's effective-dated vendor-company mapping. The two
# GET operations below are require_org_finance_or_administrator-gated, not
# require_org_administrator alone — a frontend-enablement fix (F6's own
# gap-audit check, see that dependency's own docstring in app/api/deps.py
# for the full reasoning): every other read on a vendor's detail page is
# require_org_finance-gated, and this one being stricter was a real, live
# mismatch, not a deliberate security boundary anyone had actually decided
# on. The write below (`POST .../organisation`, a genuine roster-management
# action) stays require_org_administrator-only, unchanged.
organisation_router = APIRouter(prefix="/parties/{party_id}/organisation", tags=["parties"])


@organisation_router.get("", response_model=list[PartyOrganisationMappingOut])
async def read_party_organisation_history(
    party_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(require_org_finance_or_administrator)],
) -> list:
    await _get_party(session, user.organisation_id, party_id)
    return await get_organisation_history(session, party_id)


@organisation_router.get("/current", response_model=PartyOrganisationMappingOut | None)
async def read_party_current_organisation(
    party_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(require_org_finance_or_administrator)],
):
    await _get_party(session, user.organisation_id, party_id)
    return await get_current_organisation(session, party_id)


@organisation_router.post("", response_model=PartyOrganisationMappingOut, status_code=201)
async def set_party_organisation(
    party_id: uuid.UUID,
    body: PartyOrganisationMappingSet,
    session: Annotated[AsyncSession, Depends(get_session)],
    admin: Annotated[User, Depends(require_org_administrator)],
):
    await _get_party(session, admin.organisation_id, party_id)
    await _get_party(session, admin.organisation_id, body.organisation_party_id)
    try:
        mapping = await set_current_organisation(
            session,
            organisation_id=admin.organisation_id,
            person_party_id=party_id,
            organisation_party_id=body.organisation_party_id,
            role_title=body.role_title,
            effective_from=body.effective_from,
        )
    except OrganisationMappingError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    await session.commit()
    return mapping
