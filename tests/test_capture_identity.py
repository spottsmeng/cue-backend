"""FR-NRM-03: app/capture/identity.py's real identity resolver, plus its
/admin/channel-identities API surface. Exercises the confidence-scoring
ladder (exact ChannelIdentity match -> display-name heuristic -> brand-new
Party) and the manual-override path, against real Postgres (the
channel_identities unique constraint and the FK to parties both only prove
anything under a real commit).
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.capture.identity import resolve_identity, set_manual_identity_override
from app.models import ChannelIdentity, Party
from main import app
from tests.conftest import set_org_context


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_new_external_id_mints_new_party_at_full_confidence(app_session, org_and_project):
    org_id, _project_id = org_and_project
    await set_org_context(app_session, org_id)

    resolution = await resolve_identity(
        app_session,
        organisation_id=org_id,
        channel_type="mattermost",
        external_id="user-abc123",
        display_name="Alice Tan",
    )
    await app_session.commit()

    assert resolution.confidence == 1.0
    assert resolution.created_new_party is True
    assert resolution.created_new_identity is True
    assert resolution.manually_verified is False

    party = (await app_session.execute(select(Party).where(Party.id == resolution.party_id))).scalar_one()
    assert party.display_name == "Alice Tan"
    assert party.type == "person"


@pytest.mark.asyncio
async def test_existing_channel_identity_resolves_without_rescoring(app_session, org_and_project):
    org_id, _project_id = org_and_project
    await set_org_context(app_session, org_id)

    first = await resolve_identity(
        app_session, organisation_id=org_id, channel_type="mattermost", external_id="user-abc123",
        display_name="Alice Tan",
    )
    await app_session.commit()

    second = await resolve_identity(
        app_session, organisation_id=org_id, channel_type="mattermost", external_id="user-abc123",
        display_name="Alice Tan (typo variant)",
    )
    await app_session.commit()

    assert second.party_id == first.party_id
    assert second.created_new_identity is False
    assert second.created_new_party is False
    # Resolving again does not create a second channel_identities row.
    rows = (
        await app_session.execute(
            select(ChannelIdentity).where(
                ChannelIdentity.channel_type == "mattermost", ChannelIdentity.external_id == "user-abc123"
            )
        )
    ).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_display_name_match_links_at_reduced_confidence(app_session, org_and_project):
    org_id, _project_id = org_and_project
    await set_org_context(app_session, org_id)

    existing = Party(organisation_id=org_id, display_name="Bob Lee", type="person")
    app_session.add(existing)
    await app_session.commit()

    resolution = await resolve_identity(
        app_session, organisation_id=org_id, channel_type="imap_smtp", external_id="bob@vendor.test",
        display_name="  bob lee  ",
    )
    await app_session.commit()

    assert resolution.party_id == existing.id
    assert resolution.created_new_party is False
    assert resolution.created_new_identity is True
    assert 0 < resolution.confidence < 1.0


@pytest.mark.asyncio
async def test_no_display_name_never_heuristically_links(app_session, org_and_project):
    """Without a display_name to compare, resolve_identity must not link to
    an unrelated existing Party by accident — it mints a new one."""
    org_id, _project_id = org_and_project
    await set_org_context(app_session, org_id)

    unrelated = Party(organisation_id=org_id, display_name="Carol Ng", type="person")
    app_session.add(unrelated)
    await app_session.commit()

    resolution = await resolve_identity(
        app_session, organisation_id=org_id, channel_type="whatsapp", external_id="+6591234567",
    )
    await app_session.commit()

    assert resolution.party_id != unrelated.id
    assert resolution.created_new_party is True
    assert resolution.confidence == 1.0


@pytest.mark.asyncio
async def test_manual_override_is_full_confidence_and_sticky(app_session, org_and_project):
    org_id, _project_id = org_and_project
    await set_org_context(app_session, org_id)

    wrong_party = Party(organisation_id=org_id, display_name="Wrong Match", type="person")
    correct_party = Party(organisation_id=org_id, display_name="Correct Person", type="person")
    app_session.add_all([wrong_party, correct_party])
    await app_session.commit()

    auto = await resolve_identity(
        app_session, organisation_id=org_id, channel_type="nextcloud", external_id="dave@vendor.test",
        display_name="Wrong Match",
    )
    await app_session.commit()
    assert auto.party_id == wrong_party.id
    assert auto.manually_verified is False

    await set_manual_identity_override(
        app_session, channel_type="nextcloud", external_id="dave@vendor.test", party_id=correct_party.id,
    )
    await app_session.commit()

    corrected = await resolve_identity(
        app_session, organisation_id=org_id, channel_type="nextcloud", external_id="dave@vendor.test",
    )
    assert corrected.party_id == correct_party.id
    assert corrected.confidence == 1.0
    assert corrected.manually_verified is True


@pytest.mark.asyncio
async def test_override_api_requires_org_administrator(authed_org_and_project, parties):
    org_id, _project_id, _admin, admin_token = authed_org_and_project
    vendor, _internal = parties

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/admin/channel-identities/override",
            headers=_headers(admin_token),
            json={"channel_type": "mattermost", "external_id": "override-me", "party_id": str(vendor.id)},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["party_id"] == str(vendor.id)
    assert body["manually_verified"] is True
    assert body["confidence"] == 1.0


@pytest.mark.asyncio
async def test_override_api_rejects_party_from_another_org(authed_org_and_project):
    org_id, _project_id, _admin, admin_token = authed_org_and_project

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/admin/channel-identities/override",
            headers=_headers(admin_token),
            json={"channel_type": "mattermost", "external_id": "x", "party_id": str(uuid.uuid4())},
        )
    assert response.status_code == 422
