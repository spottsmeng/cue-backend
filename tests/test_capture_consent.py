"""FR-CAP-06/07: app/capture/consent.py — the bilingual notice, written to
the consent ledger via the same upsert app/api/consent.py's own
consent_action_request now shares, and the opt-out gate item 3's pipeline
checks before persisting a Message.
"""

import pytest

from app.capture.adapters.nextcloud import NextcloudAdapter
from app.capture.consent import is_opted_out, post_consent_notice, upsert_consent_record
from app.models import Channel, ConsentRecord, Party
from tests.conftest import set_org_context


@pytest.mark.asyncio
async def test_post_consent_notice_writes_pending_record(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)

    party = Party(organisation_id=org_id, display_name="New Vendor Contact", type="person")
    channel = Channel(project_id=project_id, type="whatsapp", external_ref="group-1", healthy=True)
    app_session.add_all([party, channel])
    await app_session.commit()

    record = await post_consent_notice(
        app_session, channel=channel, party=party, to_external_id="+6591234567"
    )
    await app_session.commit()

    assert record.status == "pending"
    assert record.notice_sent_at is not None
    assert "whatsapp" in record.evidence


@pytest.mark.asyncio
async def test_is_opted_out_false_with_no_record(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    party = Party(organisation_id=org_id, display_name="Contact", type="person")
    app_session.add(party)
    await app_session.commit()

    assert await is_opted_out(app_session, party_id=party.id, project_id=project_id) is False


@pytest.mark.asyncio
async def test_is_opted_out_true_after_opt_out(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    party = Party(organisation_id=org_id, display_name="Contact", type="person")
    app_session.add(party)
    await app_session.commit()

    await upsert_consent_record(
        app_session, party_id=party.id, project_id=project_id, status="opted_out", evidence="replied OPT-OUT"
    )
    await app_session.commit()

    assert await is_opted_out(app_session, party_id=party.id, project_id=project_id) is True


@pytest.mark.asyncio
async def test_is_opted_out_false_when_merely_pending_or_accepted(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    party = Party(organisation_id=org_id, display_name="Contact", type="person")
    app_session.add(party)
    await app_session.commit()

    await upsert_consent_record(app_session, party_id=party.id, project_id=project_id, status="accepted")
    await app_session.commit()

    assert await is_opted_out(app_session, party_id=party.id, project_id=project_id) is False


@pytest.mark.asyncio
async def test_second_notice_updates_the_same_record_not_a_new_one(app_session, org_and_project):
    """FR-CAP-06 is "one-time" — the caller decides when to call this
    (item 3's pipeline only calls it on a brand-new identity), but the
    ledger write itself is still upsert-by-(party, project), so even a
    caller that invokes it twice never fragments one party's consent state
    into two rows."""
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    party = Party(organisation_id=org_id, display_name="Contact", type="person")
    channel = Channel(project_id=project_id, type="whatsapp", external_ref="group-1", healthy=True)
    app_session.add_all([party, channel])
    await app_session.commit()

    first = await post_consent_notice(app_session, channel=channel, party=party, to_external_id="x")
    await app_session.commit()
    second = await post_consent_notice(app_session, channel=channel, party=party, to_external_id="x")
    await app_session.commit()

    assert first.id == second.id


@pytest.mark.asyncio
async def test_file_storage_capability_adapter_has_no_send_concept():
    """Consent has no meaning for a channel with no conversation to consent
    to — the real NextcloudAdapter's send() raises NotImplementedError
    (existing, correct ChannelAdapter.send() behaviour, per its own
    docstring), which is why item 3's pipeline is expected to simply never
    call post_consent_notice for a file_storage channel at all."""
    adapter = NextcloudAdapter()
    channel = Channel(type="nextcloud", external_ref="/CUE")
    with pytest.raises(NotImplementedError):
        await adapter.send(channel, "irrelevant", "irrelevant")
