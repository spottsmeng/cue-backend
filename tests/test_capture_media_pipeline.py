"""Item 6: app/capture/media_pipeline.py — fetch -> store original (real
MinIO, docker-compose.yml's `minio` service, same "against real
infrastructure, not mocks" posture tests/test_documents_api.py already
establishes for the same StorageBackend) -> derive (thumbnail/OCR/EXIF for
images, extracted text for documents). The adapter itself is faked (only
`fetch_media` is ever called by this module — a full live ChannelAdapter
round trip is tests/test_capture_adapters_live.py's job, already covered
there for Mattermost/Nextcloud/IMAP-SMTP).
"""

import io
import uuid
from datetime import datetime, timezone as dt_timezone

import pytest
from PIL import Image, ImageDraw
from sqlalchemy import select

from app.capture.media_pipeline import process_message_media, process_pending_media_for_message
from app.capture.models import Message, MessageMedia
from app.documents.storage import get_storage_backend
from app.models import Channel, Project
from tests.conftest import set_org_context

_MINIMAL_PDF = b"""%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj
3 0 obj<</Type/Page/Parent 2 0 R/Resources<</Font<</F1 4 0 R>>>>/MediaBox[0 0 200 200]/Contents 5 0 R>>endobj
4 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj
5 0 obj<</Length 58>>
stream
BT /F1 18 Tf 20 100 Td (Hello CUE PDF test) Tj ET
endstream
endobj
xref
0 6
trailer<</Size 6/Root 1 0 R>>
%%EOF"""


def _rendered_image_with_exif(text: str) -> bytes:
    image = Image.new("RGB", (600, 100), color="white")
    ImageDraw.Draw(image).text((10, 30), text, fill="black")
    exif = image.getexif()
    exif[0x0132] = "2026:06:22 09:00:00"
    buf = io.BytesIO()
    image.save(buf, format="jpeg", exif=exif)
    return buf.getvalue()


class _FakeMediaAdapter:
    """Only `fetch_media` is ever called by media_pipeline.py — nothing
    else on the ChannelAdapter Protocol is exercised by this module."""

    def __init__(self, media_bytes: dict[str, bytes], *, raise_not_implemented_for: set[str] | None = None):
        self._media_bytes = media_bytes
        self._raise_for = raise_not_implemented_for or set()
        self.fetch_calls: list[str] = []

    async def fetch_media(self, channel: Channel, uri: str) -> bytes:
        self.fetch_calls.append(uri)
        if uri in self._raise_for:
            raise NotImplementedError("no media fetch for this fake channel type")
        return self._media_bytes[uri]


async def _make_message_and_channel(app_session, project_id) -> tuple[Message, Channel, Project]:
    project = (await app_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    channel = Channel(project_id=project_id, type="mattermost", external_ref="c1", healthy=True)
    app_session.add(channel)
    await app_session.flush()
    message = Message(
        project_id=project_id, channel_id=channel.id, external_id="m1", sender_external_id="user-x",
        sent_at=datetime.now(dt_timezone.utc),
        payload_hash=f"hash-{uuid.uuid4()}",
    )
    app_session.add(message)
    await app_session.commit()
    return message, channel, project


@pytest.mark.asyncio
async def test_process_message_media_image_stores_original_thumbnail_ocr_exif(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    message, channel, project = await _make_message_and_channel(app_session, project_id)

    image_bytes = _rendered_image_with_exif("SITE PHOTO OK")
    media = MessageMedia(message_id=message.id, kind="image", source_uri="file-1", original_filename="photo.jpg")
    app_session.add(media)
    await app_session.commit()

    adapter = _FakeMediaAdapter({"file-1": image_bytes})
    storage = get_storage_backend()
    await process_message_media(app_session, project=project, channel=channel, adapter=adapter, media=media, storage=storage)
    await app_session.commit()

    assert media.storage_key is not None
    assert media.thumbnail_key is not None
    # Tesseract's exact whitespace can vary with the default PIL font's
    # kerning at small sizes — compare with whitespace collapsed rather than
    # asserting an exact character-for-character match.
    assert "SITEPHOTOOK" in (media.ocr_text or "").replace(" ", "").replace("\n", "")
    assert media.exif is not None
    assert media.exif["datetime_original"] == "2026:06:22 09:00:00"

    stored_original = await storage.get(media.storage_key)
    assert stored_original == image_bytes
    stored_thumb = await storage.get(media.thumbnail_key)
    assert stored_thumb != image_bytes
    # The thumbnail is itself a real, decodable JPEG, not just arbitrary bytes.
    Image.open(io.BytesIO(stored_thumb)).verify()


@pytest.mark.asyncio
async def test_process_message_media_document_stores_and_extracts_text(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    message, channel, project = await _make_message_and_channel(app_session, project_id)

    media = MessageMedia(
        message_id=message.id, kind="document", source_uri="file-2", original_filename="quote.pdf"
    )
    app_session.add(media)
    await app_session.commit()

    adapter = _FakeMediaAdapter({"file-2": _MINIMAL_PDF})
    storage = get_storage_backend()
    await process_message_media(app_session, project=project, channel=channel, adapter=adapter, media=media, storage=storage)
    await app_session.commit()

    assert media.storage_key is not None
    assert media.ocr_text is not None
    assert "Hello CUE PDF test" in media.ocr_text
    assert media.thumbnail_key is None  # not an image — no thumbnail derived


@pytest.mark.asyncio
async def test_process_message_media_is_idempotent(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    message, channel, project = await _make_message_and_channel(app_session, project_id)

    media = MessageMedia(message_id=message.id, kind="document", source_uri="file-3", original_filename="q.pdf")
    app_session.add(media)
    await app_session.commit()

    adapter = _FakeMediaAdapter({"file-3": _MINIMAL_PDF})
    storage = get_storage_backend()
    await process_message_media(app_session, project=project, channel=channel, adapter=adapter, media=media, storage=storage)
    await process_message_media(app_session, project=project, channel=channel, adapter=adapter, media=media, storage=storage)
    await app_session.commit()

    assert adapter.fetch_calls == ["file-3"]  # second call was a no-op, storage_key already set


@pytest.mark.asyncio
async def test_process_message_media_handles_unsupported_fetch_gracefully(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    message, channel, project = await _make_message_and_channel(app_session, project_id)

    media = MessageMedia(message_id=message.id, kind="document", source_uri="file-4", original_filename="q.pdf")
    app_session.add(media)
    await app_session.commit()

    adapter = _FakeMediaAdapter({}, raise_not_implemented_for={"file-4"})
    storage = get_storage_backend()
    await process_message_media(app_session, project=project, channel=channel, adapter=adapter, media=media, storage=storage)
    await app_session.commit()

    assert media.storage_key is None
    assert media.ocr_text is None


@pytest.mark.asyncio
async def test_process_pending_media_for_message_processes_only_unprocessed_rows(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    message, channel, project = await _make_message_and_channel(app_session, project_id)

    already_done = MessageMedia(
        message_id=message.id, kind="document", source_uri="file-5", original_filename="x.pdf",
        storage_key="capture/already/done",
    )
    pending = MessageMedia(message_id=message.id, kind="document", source_uri="file-6", original_filename="y.pdf")
    app_session.add_all([already_done, pending])
    await app_session.commit()

    adapter = _FakeMediaAdapter({"file-6": _MINIMAL_PDF})
    storage = get_storage_backend()
    processed = await process_pending_media_for_message(
        app_session, project=project, channel=channel, adapter=adapter, message=message, storage=storage
    )
    await app_session.commit()

    assert processed == 1
    assert adapter.fetch_calls == ["file-6"]

    rows = (
        await app_session.execute(select(MessageMedia).where(MessageMedia.message_id == message.id))
    ).scalars().all()
    assert all(r.storage_key is not None for r in rows)


@pytest.mark.asyncio
async def test_document_media_resolving_to_a_real_document_checks_drift(app_session, org_and_project):
    """FR-DOC-09 wiring: a document-kind MessageMedia whose filename
    matches a real Document triggers app/documents/drift.py's real check —
    end to end through process_message_media, not drift.py in isolation
    (already covered by tests/test_documents_drift.py)."""
    from app.documents.service import create_document
    from app.foresight.models import Deviation
    from app.models import Evidence

    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    message, channel, project = await _make_message_and_channel(app_session, project_id)

    evidence = Evidence(
        channel="manual", sent_at=datetime.now(dt_timezone.utc), language="en", original_text="seed",
    )
    storage = get_storage_backend()
    await create_document(
        app_session, project=project, name="site-photo-manifest.pdf", storage=storage,
        file_bytes=b"approved manifest bytes", content_type="application/pdf",
        extracted_text="stand-in", class_code=None, evidence=evidence, actor_id=None,
        filename="site-photo-manifest.pdf",
    )
    await app_session.commit()

    media = MessageMedia(
        message_id=message.id, kind="document", source_uri="file-drift",
        original_filename="site-photo-manifest.pdf",
    )
    app_session.add(media)
    await app_session.commit()

    adapter = _FakeMediaAdapter({"file-drift": b"a DIFFERENT circulated version"})
    await process_message_media(
        app_session, project=project, channel=channel, adapter=adapter, media=media, storage=storage,
    )
    await app_session.commit()

    deviations = (
        await app_session.execute(select(Deviation).where(Deviation.project_id == project_id))
    ).scalars().all()
    assert len(deviations) == 1
