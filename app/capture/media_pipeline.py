"""Prompt 11 item 6: FR-CAP-12's original-media-preservation pipeline — a
queue consumer (like the normaliser and identity resolver, never called
inline from a ChannelAdapter), not the adapter's own concern. Reuses
app/documents/storage.py's StorageBackend Protocol for the original bytes
(FR-CAP-12: "preserve original media at full fidelity... without mutating
the original") rather than building a second storage abstraction — the same
instruction Prompt 11 gives explicitly. Derived artefacts (thumbnails, OCR
text, EXIF) are separate MessageMedia columns, filled in after the original
is safely stored, never overwrites of it.

Also where FR-DOC-09 (item 12) closes: once a document-kind attachment's
bytes are in hand, app/documents/drift.py checks whether it resolves to a
known Document and, if so, whether it matches that Document's approved
version — see that module's own docstring for why this is the session that
finally closes it.
"""

import io
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.capture.adapters.base import ChannelAdapter
from app.capture.asr import ASRClient, get_default_asr_client
from app.capture.media import OCRClient, extract_document_text, extract_exif, get_default_ocr_client
from app.capture.models import Message, MessageMedia
from app.documents.drift import check_document_drift, resolve_document_for_attachment
from app.documents.storage import StorageBackend
from app.models import Channel, Project

logger = logging.getLogger("cue.capture.media_pipeline")

_THUMBNAIL_SIZE = (320, 320)
# StorageBackend.put wants a content_type, but no adapter's RawCapturedMedia
# carries one (only kind/uri/filename) — a generic octet-stream is correct
# and honest for the *original* bytes regardless of kind; a consumer that
# needs the real type (e.g. serving it back to a browser) has the original
# filename on the row to infer from, the same way a plain file download
# would.
_ORIGINAL_CONTENT_TYPE = "application/octet-stream"


def _make_thumbnail(image_bytes: bytes) -> bytes | None:
    """FR-CAP-12's "generate derived thumbnails... without mutating the
    original" — a real, small JPEG derivative via Pillow. Returns None for
    anything Pillow can't decode as an image (a corrupt upload, or a
    MessageMedia row misclassified as "image" by an adapter that only had a
    filename to guess from) rather than raising — the original is already
    safely stored by the time this runs, so a thumbnail failure is a
    degradation, not data loss."""
    try:
        from PIL import Image

        image = Image.open(io.BytesIO(image_bytes))
        image.thumbnail(_THUMBNAIL_SIZE)
        buf = io.BytesIO()
        image.convert("RGB").save(buf, format="JPEG", quality=85)
        return buf.getvalue()
    except Exception:
        logger.info("thumbnail generation skipped — not a decodable image", exc_info=True)
        return None


async def process_message_media(
    session: AsyncSession,
    *,
    project: Project,
    channel: Channel,
    adapter: ChannelAdapter,
    media: MessageMedia,
    storage: StorageBackend,
    ocr_client: OCRClient | None = None,
    asr_client: ASRClient | None = None,
    language_hint: str | None = None,
    message_text: str | None = None,
) -> None:
    """Fetches `media.source_uri` via the adapter, stores the original
    bytes (FR-CAP-12), then derives what applies to `media.kind`:
    image -> thumbnail + OCR text (FR-NRM-05) + EXIF (FR-NRM-06); document
    -> extracted text (FR-NRM-05); voice_note -> transcript + per-utterance
    confidence (FR-VOI-01/04, item 7's app/capture/asr.py — `language_hint`
    is the parent Message's own detected language, app/capture/normalise.py's
    detect_language, so a code-switched chat context routes to SenseVoice
    the same way FR-VOI-02 asks for at the ASR layer too).

    Idempotent: a `media` row that already has `storage_key` set is a no-op
    — same "already processed, don't redo it" guard
    app/capture/pipeline.py's `extraction_attempted_at` establishes for
    extraction, applied here to media processing under the same
    at-least-once arq redelivery concern.

    Failure is contained per-media, not propagated — a channel/adapter
    combination with no real fetch_media (WeChat Work; the fixture backend)
    or a transient fetch failure logs and leaves this row's derived fields
    NULL rather than failing the whole channel's ingestion; FR-CAP-12's
    guarantee is about the messages the pipeline *can* reach, not a promise
    every adapter can serve media today (mirrors send()'s own
    NotImplementedError-for-a-capability-gap posture at the adapter level,
    contained here at the pipeline level instead).
    """
    if media.storage_key is not None:
        return

    try:
        data = await adapter.fetch_media(channel, media.source_uri or "")
    except NotImplementedError as e:
        logger.info("media fetch not supported for channel_type=%s: %s", channel.type, e)
        return
    except Exception:
        logger.warning("media fetch failed for message_media=%s", media.id, exc_info=True)
        return

    storage_key = f"capture/{media.message_id}/{media.id}/{media.original_filename or 'original'}"
    await storage.put(storage_key, data, _ORIGINAL_CONTENT_TYPE)
    media.storage_key = storage_key

    if media.kind == "image":
        thumbnail = _make_thumbnail(data)
        if thumbnail is not None:
            thumbnail_key = f"capture/{media.message_id}/{media.id}/thumbnail.jpg"
            await storage.put(thumbnail_key, thumbnail, "image/jpeg")
            media.thumbnail_key = thumbnail_key
        try:
            client = ocr_client or get_default_ocr_client()
            media.ocr_text = (await client.extract_text(data)) or None
        except Exception:
            logger.warning("OCR failed for message_media=%s", media.id, exc_info=True)
        media.exif = extract_exif(data)
    elif media.kind == "document":
        media.ocr_text = extract_document_text(media.original_filename, data)
        # FR-DOC-09 (item 12): best-effort — an attachment that doesn't
        # resolve to a known Document is not an error, just not checked
        # (app/documents/drift.py's own docstring).
        document = await resolve_document_for_attachment(
            session, project_id=project.id, filename=media.original_filename
        )
        if document is not None:
            await check_document_drift(
                session, project=project, document=document, attachment_bytes=data, storage=storage,
                original_text=message_text,
            )
    elif media.kind == "voice_note":
        try:
            client = asr_client or get_default_asr_client(language_hint)
            result = await client.transcribe(data)
        except Exception:
            logger.warning("ASR failed for message_media=%s", media.id, exc_info=True)
        else:
            media.transcript = result.text or None
            media.transcript_language = result.language
            media.transcript_confidence = result.mean_confidence
            media.transcript_detail = [
                {"text": s.text, "start": s.start, "end": s.end, "confidence": s.confidence}
                for s in result.segments
            ]

    await session.flush()


async def process_pending_media_for_message(
    session: AsyncSession,
    *,
    project: Project,
    channel: Channel,
    adapter: ChannelAdapter,
    message: Message,
    storage: StorageBackend,
    ocr_client: OCRClient | None = None,
    asr_client: ASRClient | None = None,
) -> int:
    """Every not-yet-processed MessageMedia row for one Message — called
    from app/capture/pipeline.py's own per-message step, before extraction
    runs, so media processing rides the same in-order, per-channel arq job
    item 3's worker already establishes rather than a separate queue of its
    own.

    A pure voice-note message (`message.text` empty — nothing but audio)
    gets its transcript copied up onto `message.text` once ASR succeeds —
    CUE-PRD.md's own FR-CAP-13 language for meeting transcripts ("ingest
    transcript text into the same extraction and retrieval pipeline as chat
    messages") applies just as directly to a voice note: without this, a
    voice-note-only message would sit in the ledger forever with nothing for
    app/capture/pipeline.py's extract_from_message to extract from, since
    that function has no separate "check for a transcript" branch of its own
    — the message envelope's own `text` field is the single input it reads,
    by design, for both origins.
    """
    pending = (
        await session.execute(
            select(MessageMedia).where(
                MessageMedia.message_id == message.id, MessageMedia.storage_key.is_(None)
            )
        )
    ).scalars().all()
    for media in pending:
        await process_message_media(
            session,
            project=project,
            channel=channel,
            adapter=adapter,
            media=media,
            storage=storage,
            ocr_client=ocr_client,
            asr_client=asr_client,
            language_hint=message.language,
            message_text=message.text,
        )
        if media.kind == "voice_note" and media.transcript and not message.text:
            message.text = media.transcript
            message.language = media.transcript_language or message.language
            await session.flush()
    return len(pending)
