"""FR-NRM-01/02 (PRD §6.2): the normaliser — a queue consumer (item 3), never
called inline from a ChannelAdapter (app/capture/adapters/base.py's own
docstring). Turns one adapter's RawCapturedMessage (app/capture/schema.py)
into a durable Message row (app/capture/models.py): dedup by payload_hash
(FR-CAP-11), identity resolution (FR-NRM-03, app/capture/identity.py),
consent gating (FR-CAP-07, app/capture/consent.py) and language detection
(FR-NRM-02) all happen here, in that order, before a single row is written —
NFR-AVL-02 ("capture must never lose a message") is why identity resolution
never blocks storage (an unresolved sender still gets a Message row, just
with confidence left at whatever resolve_identity returned), and FR-CAP-07
is the one deliberate exception to that rule (an opted-out participant's
message is not stored at all, by explicit product requirement).
"""

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.capture.consent import is_opted_out
from app.capture.identity import resolve_identity
from app.capture.models import Message, MessageMedia
from app.capture.schema import RawCapturedMessage
from app.models import Channel, Project

# FR-NRM-02: "Detect message language per message, including mixed-language
# and code-switched content." A Unicode-script-ratio heuristic, not a
# statistical language-ID model — deliberately, for two reasons. First, it
# is the one approach that directly answers "is this code-switched", which
# is exactly what FR-NRM-02 asks for and what a single-best-guess language-ID
# library (langdetect, fastText, etc.) structurally cannot express — those
# return one language, never "both, mixed". Second, this domain's actual
# code-switching pattern (CUE-Tech-Stack.md §2.4, on why SenseVoice was
# named for FR-VOI-02: "English terms embedded in Chinese speech is the
# norm") is specifically Latin-script English terms inside Han-script
# Chinese text — a script-ratio test catches exactly that pattern by
# construction, not by training data coverage. Not a claim this generalises
# to language pairs sharing a script (e.g. English/French); this domain's
# two real languages don't.
_HAN_RE = re.compile(r"[一-鿿㐀-䶿]")
_LATIN_WORD_RE = re.compile(r"[A-Za-z]{2,}")
# Below this fraction of the minority script's presence, treat it as
# incidental (a lone acronym or product name in an otherwise single-language
# message) rather than genuine code-switching.
_CODE_SWITCH_THRESHOLD = 0.15


def detect_language(text: str | None) -> str:
    """Returns a bcp47 tag ('en', 'zh'), the synthetic 'zh+en' code-switch
    tag this module defines (no standard bcp47 tag exists for "genuinely
    mixed within one message"), or 'und' (bcp47's own "undetermined") for
    empty/non-linguistic text (e.g. a media-only message with no caption)."""
    if not text:
        return "und"
    han = len(_HAN_RE.findall(text))
    latin = len(_LATIN_WORD_RE.findall(text))
    if han == 0 and latin == 0:
        return "und"
    if han == 0:
        return "en"
    if latin == 0:
        return "zh"
    minority_fraction = min(han, latin) / (han + latin)
    if minority_fraction >= _CODE_SWITCH_THRESHOLD:
        return "zh+en"
    return "zh" if han >= latin else "en"


async def _find_by_payload_hash(
    session: AsyncSession, *, project_id: uuid.UUID, payload_hash: str
) -> Message | None:
    return (
        await session.execute(
            select(Message).where(Message.project_id == project_id, Message.payload_hash == payload_hash)
        )
    ).scalar_one_or_none()


@dataclass
class IngestResult:
    """Distinguishes the three outcomes app/capture/pipeline.py's summary
    (and item 10's gap/backfill stats) need to tell apart — a plain
    `Message | None` return can't distinguish "duplicate of an
    already-processed message" from "brand-new" without a second query."""

    message: Message | None
    is_new: bool
    opted_out: bool


async def normalise_and_ingest(
    session: AsyncSession, *, project: Project, channel: Channel, raw: RawCapturedMessage
) -> IngestResult:
    """`message` is None only when `opted_out` is True (FR-CAP-07: this
    message must not enter the queue beyond this point at all). Idempotent
    under at-least-once redelivery: a payload_hash already seen for this
    project returns the existing row (`is_new=False`) rather than inserting
    a second one, whether caught by the pre-check (the common case) or the
    UniqueConstraint itself (a genuine race between two workers processing
    the same backlog concurrently).
    """
    existing = await _find_by_payload_hash(
        session, project_id=project.id, payload_hash=raw.raw_payload_hash
    )
    if existing is not None:
        return IngestResult(message=existing, is_new=False, opted_out=False)

    resolution = await resolve_identity(
        session,
        organisation_id=project.organisation_id,
        channel_type=channel.type,
        external_id=raw.sender_external_id,
    )

    if await is_opted_out(session, party_id=resolution.party_id, project_id=project.id):
        return IngestResult(message=None, is_new=False, opted_out=True)

    message = Message(
        project_id=project.id,
        channel_id=channel.id,
        external_id=raw.external_id,
        sender_external_id=raw.sender_external_id,
        author_party_id=resolution.party_id,
        identity_confidence=resolution.confidence,
        identity_manually_verified=resolution.manually_verified,
        sent_at=raw.sent_at,
        language=detect_language(raw.text),
        text=raw.text,
        payload_hash=raw.raw_payload_hash,
    )
    session.add(message)
    try:
        await session.flush()
    except IntegrityError:
        # Lost a race with another worker on the same (project_id,
        # payload_hash) — the other worker's row is now the durable one.
        await session.rollback()
        existing = await _find_by_payload_hash(
            session, project_id=project.id, payload_hash=raw.raw_payload_hash
        )
        if existing is None:
            raise  # not actually a payload_hash collision — re-raise the original problem
        return IngestResult(message=existing, is_new=False, opted_out=False)

    for media in raw.media:
        session.add(
            MessageMedia(
                message_id=message.id,
                kind=media.kind,
                original_filename=media.filename,
                source_uri=media.uri,
            )
        )
    await session.flush()
    return IngestResult(message=message, is_new=True, opted_out=False)
