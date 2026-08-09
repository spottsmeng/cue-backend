"""FR-NRM-01/02: app/capture/normalise.py — language detection (including
code-switching) and the normalise-and-ingest pipeline step (dedup, identity
resolution, consent gating)."""

import uuid
from datetime import datetime, timezone as dt_timezone

import pytest
from sqlalchemy import select

from app.capture.consent import upsert_consent_record
from app.capture.models import Message, MessageMedia
from app.capture.normalise import detect_language, normalise_and_ingest
from app.capture.schema import RawCapturedMedia, RawCapturedMessage, compute_payload_hash
from app.models import Channel, Party, Project
from tests.conftest import set_org_context


@pytest.mark.parametrize(
    "text,expected",
    [
        (None, "und"),
        ("", "und"),
        ("Please confirm the LED screen delivery by Friday.", "en"),
        ("请确认周五之前交付LED屏幕", "zh"),
        ("请确认 LED screen 的 delivery time 周五之前", "zh+en"),
        ("OK", "en"),
        ("好的", "zh"),
    ],
)
def test_detect_language(text, expected):
    assert detect_language(text) == expected


def _raw(channel_type: str, external_id: str, sender: str, text: str, sent_at=None, media=None):
    sent_at = sent_at or datetime(2026, 6, 1, 9, 0, tzinfo=dt_timezone.utc)
    raw_bytes = text.encode("utf-8")
    return RawCapturedMessage(
        external_id=external_id,
        sender_external_id=sender,
        sent_at=sent_at,
        text=text,
        raw_payload_hash=compute_payload_hash(channel_type, external_id, raw_bytes),
        media=media or [],
    )


@pytest.mark.asyncio
async def test_ingest_creates_message_and_resolves_identity(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    channel = Channel(project_id=project_id, type="whatsapp", external_ref="g1", healthy=True)
    app_session.add(channel)
    await app_session.commit()

    raw = _raw("whatsapp", "msg-1", "+6591234567", "Confirming delivery on Friday.")
    result = await normalise_and_ingest(app_session, project=project, channel=channel, raw=raw)
    await app_session.commit()

    assert result.is_new is True
    assert result.opted_out is False
    message = result.message
    assert message is not None
    assert message.language == "en"
    assert message.author_party_id is not None
    assert message.identity_confidence == 1.0
    assert message.payload_hash == raw.raw_payload_hash


@pytest.mark.asyncio
async def test_ingest_is_idempotent_under_redelivery(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    channel = Channel(project_id=project_id, type="whatsapp", external_ref="g1", healthy=True)
    app_session.add(channel)
    await app_session.commit()

    raw = _raw("whatsapp", "msg-1", "+6591234567", "Confirming delivery on Friday.")
    first = await normalise_and_ingest(app_session, project=project, channel=channel, raw=raw)
    await app_session.commit()
    second = await normalise_and_ingest(app_session, project=project, channel=channel, raw=raw)
    await app_session.commit()

    assert first.is_new is True
    assert second.is_new is False
    assert first.message.id == second.message.id
    count = (
        await app_session.execute(select(Message).where(Message.payload_hash == raw.raw_payload_hash))
    ).scalars().all()
    assert len(count) == 1


@pytest.mark.asyncio
async def test_ingest_drops_message_for_opted_out_party(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    channel = Channel(project_id=project_id, type="whatsapp", external_ref="g1", healthy=True)
    app_session.add(channel)
    await app_session.commit()

    # First message establishes the identity so we have a party_id to opt out.
    raw1 = _raw("whatsapp", "msg-1", "+6598765432", "Hello, quoting the job now.")
    first = await normalise_and_ingest(app_session, project=project, channel=channel, raw=raw1)
    await app_session.commit()
    assert first.message is not None

    await upsert_consent_record(
        app_session, party_id=first.message.author_party_id, project_id=project_id, status="opted_out",
    )
    await app_session.commit()

    raw2 = _raw("whatsapp", "msg-2", "+6598765432", "Second message after opting out.")
    second = await normalise_and_ingest(app_session, project=project, channel=channel, raw=raw2)
    await app_session.commit()

    assert second.message is None
    assert second.opted_out is True
    stored = (
        await app_session.execute(select(Message).where(Message.external_id == "msg-2"))
    ).scalar_one_or_none()
    assert stored is None


@pytest.mark.asyncio
async def test_ingest_creates_message_media_rows(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    channel = Channel(project_id=project_id, type="mattermost", external_ref="c1", healthy=True)
    app_session.add(channel)
    await app_session.commit()

    raw = _raw(
        "mattermost", "msg-3", "user-x", "See attached photo",
        media=[RawCapturedMedia(kind="image", uri="file-id-123", filename="site.jpg")],
    )
    result = await normalise_and_ingest(app_session, project=project, channel=channel, raw=raw)
    await app_session.commit()
    message = result.message

    media_rows = (
        await app_session.execute(select(MessageMedia).where(MessageMedia.message_id == message.id))
    ).scalars().all()
    assert len(media_rows) == 1
    assert media_rows[0].source_uri == "file-id-123"
    assert media_rows[0].storage_key is None
