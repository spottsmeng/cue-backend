"""FR-CAP-06/07: app/capture/consent.py — the bilingual notice, written to
the consent ledger via the same upsert app/api/consent.py's own
consent_action_request now shares, and the opt-out gate item 3's pipeline
checks before persisting a Message.
"""

import pytest

from app.capture.adapters.nextcloud import NextcloudAdapter, NextcloudCaptureSettings
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
    call post_consent_notice for a file_storage channel at all.

    Settings passed explicitly rather than read from ambient env/.env: this
    is a unit test of send()'s NotImplementedError, not a live-connectivity
    test (tests/test_capture_adapters_live.py owns that, skipif-gated on
    real CUE_NEXTCLOUD_* credentials) — it shouldn't fail in CI just because
    no .env exists there, and it shouldn't risk flipping the live test's
    skip condition by relying on real-looking env vars either."""
    adapter = NextcloudAdapter(
        NextcloudCaptureSettings(base_url="http://ci.invalid", username="ci", app_password="ci")
    )
    channel = Channel(type="nextcloud", external_ref="/CUE")
    with pytest.raises(NotImplementedError):
        await adapter.send(channel, "irrelevant", "irrelevant")


@pytest.mark.asyncio
async def test_consent_notice_names_the_tenant_not_a_hardcoded_pico(
    app_session, org_and_project
):
    """This text is sent to real outside parties and states on whose behalf
    their messages are being recorded. "Pico" was a literal in it.

    On any other tenant that is not a cosmetic naming slip — it is a legal
    notice naming the wrong data controller, which is worse than no notice,
    because a vendor who replied ACCEPT would have consented to something
    untrue. Both language halves have to carry the right name, not just the
    English one.
    """
    from sqlalchemy import select

    from app.capture.consent import build_consent_notice
    from app.models import Organisation

    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    org = (
        await app_session.execute(select(Organisation).where(Organisation.id == org_id))
    ).scalar_one()

    notice = await build_consent_notice(app_session, project_id=project_id)

    assert "Pico" not in notice
    assert notice.count(org.name) == 2  # the English half and the 中文 half
    assert "{organisation}" not in notice
    assert "ACCEPT" in notice and "OPT-OUT" in notice


@pytest.mark.asyncio
async def test_consent_notice_falls_back_rather_than_failing_to_send(app_session):
    """A notice that goes out slightly generic is recoverable. One that fails
    to go out is an FR-CAP-06 breach and blocks capture for that party, so an
    unresolvable organisation must not raise."""
    import uuid as _uuid

    from app.capture.consent import build_consent_notice

    notice = await build_consent_notice(app_session, project_id=_uuid.uuid4())
    assert "the project operator" in notice
    assert "{organisation}" not in notice
