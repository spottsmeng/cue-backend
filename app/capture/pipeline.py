"""Item 3's orchestration layer — wires the normaliser (app/capture/normalise.py)
and the existing extraction contract (app/ledger/extractor.py, via
app/capture/extraction_bridge.py) together into the one call both a backfill
(item 10) and a scheduled-window trigger (item 11) use. The arq job itself
(app/capture/worker.py) is a thin wrapper around ingest_channel_backlog below
— this module has no arq/Redis dependency of its own, so it's directly
callable (and directly testable) without a running worker or broker, the same
"the cron job body is also directly callable" shape
app/foresight/worker.py's run_foresight_sweep already establishes.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone as dt_timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.capture.adapters.base import ChannelAdapter
from app.capture.extraction_bridge import build_case, build_project_context
from app.capture.media_pipeline import process_pending_media_for_message
from app.capture.models import Message, MessageMedia
from app.capture.normalise import normalise_and_ingest
from app.capture.schema import RawCapturedMessage
from app.core.db import org_scoped_transaction
from app.documents.storage import StorageBackend, get_storage_backend
from app.ledger.context import load_open_commitment_context
from app.ledger.extractor import RejectedExtraction, extract_case
from app.llm.client import ModelClient
from app.models import Channel, ChannelType, Evidence, Party, Project
from app.writeback.reply import handle_potential_reply

logger = logging.getLogger("cue.capture.pipeline")


@dataclass
class IngestionSummary:
    received: int = 0
    new_messages: int = 0
    duplicates: int = 0
    opted_out: int = 0
    commitments_created: int = 0
    extractions_rejected: int = 0
    media_processed: int = 0
    latest_sent_at: datetime | None = None


async def _party_display_name(session: AsyncSession, party_id: uuid.UUID | None) -> str | None:
    if party_id is None:
        return None
    party = (await session.execute(select(Party).where(Party.id == party_id))).scalar_one_or_none()
    return party.display_name if party else None


async def _channel_capability(session: AsyncSession, channel_type_code: str) -> str | None:
    """`channels.type` FKs `channel_types.code` (app/models/channel_type.py),
    not the row's `capability` directly — a real DB lookup, not the static
    seed mapping app/capture/fixtures.py uses for offline cases.json cases,
    since a tenant can register its own channel_types row (organisation_id
    set) that the seed list knows nothing about."""
    return (
        await session.execute(
            select(ChannelType.capability).where(ChannelType.code == channel_type_code)
        )
    ).scalar_one_or_none()


async def _finalise_message_evidence(
    session: AsyncSession, *, message: Message, evidence_ids: list[uuid.UUID], storage: StorageBackend
) -> None:
    """Two things extract_case has no way to do, since it only ever sees a
    case dict, never a real Message row:

    1. Backfills `Evidence.message_id` to this real Message — item 1's own
       point (a real FK, replacing "Points at the future Message/capture
       table — not modelled yet") is hollow if nothing ever actually sets
       the column on a real-capture Evidence row.

       Keyed on the Evidence ids this extraction just wrote, not on its
       commitment ids. Those were the same thing only while a commitment
       could never gain evidence from more than one message; extraction's
       `relates_to` path (a later message citing an already-logged
       commitment) makes that false, and selecting by commitment_id would
       re-point the *earlier* message's evidence at this one.
    2. FR-VOI-05 ("make it playable from any evidence link") /
       FR-VOI-04 (per-utterance confidence, "voice only"): if this message
       came from a voice note, every Evidence row extract_case just wrote
       gets `media_ref` (a signed URL into the same StorageBackend item 6
       already stored the original audio in) and `transcript_confidence`
       (copied from the processed MessageMedia row) — untouched for a
       text-origin message.
    """
    if not evidence_ids:
        return
    evidence_rows = (
        await session.execute(select(Evidence).where(Evidence.id.in_(evidence_ids)))
    ).scalars().all()
    if not evidence_rows:
        return

    for evidence in evidence_rows:
        evidence.message_id = message.id

    voice_media = (
        await session.execute(
            select(MessageMedia).where(
                MessageMedia.message_id == message.id,
                MessageMedia.kind == "voice_note",
                MessageMedia.storage_key.is_not(None),
            )
        )
    ).scalars().first()
    if voice_media is not None:
        media_ref = storage.signed_url(voice_media.storage_key)
        for evidence in evidence_rows:
            evidence.media_ref = media_ref
            evidence.transcript_confidence = voice_media.transcript_confidence

    await session.flush()


async def extract_from_message(
    session: AsyncSession,
    *,
    project: Project,
    channel: Channel,
    message: Message,
    client: ModelClient | None = None,
    storage: StorageBackend | None = None,
    pinned_commitment_ids: tuple[uuid.UUID, ...] = (),
) -> int:
    """Runs the extraction contract (app/ledger/extractor.py's extract_case)
    against one real captured Message. Guards against ever extracting the
    same message twice (`extraction_attempted_at`) — arq's at-least-once
    redelivery would otherwise be able to re-run this and create duplicate
    Commitment rows. That guarantee is this function's job: extract_case
    dedups *within* one message (a returned item pointing at an
    already-logged commitment links to it instead of duplicating it) but has
    no notion of the same message arriving twice.

    Returns the number of commitments created — always 0 for a text-less
    message (nothing to extract from; still marks the attempt so it's never
    retried), a rejected extraction, or a message already attempted.

    `client` defaults to extract_case's own configured extraction-role
    client (Ollama/Anthropic per .env) — overridable so tests exercise this
    module without a live model, the same override extract_case itself
    already supports for the same reason.

    `pinned_commitment_ids` are commitments that must appear in the
    already-logged context extraction is shown, whatever the recency window
    would otherwise select — see ingest_raw_message, which pins the
    commitment a write-back reply was just matched to.

    The extraction itself runs inside a SAVEPOINT (`session.begin_nested()`,
    the same isolation app/capture/normalise.py and app/llm/cost.py already
    use). extract_case verifies before it writes, so it does not leave
    partial state of its own — but the Message row was inserted in this same
    transaction, and NFR-AVL-02 ("capture must never lose a message") means
    a failure anywhere under extraction must not be able to take the captured
    message down with it. The savepoint is what makes "roll back the
    extraction, keep the message" expressible at all.
    """
    if message.extraction_attempted_at is not None:
        return 0

    if not message.text:
        message.extraction_attempted_at = datetime.now(dt_timezone.utc)
        await session.flush()
        return 0

    party_name = await _party_display_name(session, message.author_party_id)
    capability = await _channel_capability(session, channel.type)
    context = await build_project_context(session, project)
    ledger_context = await load_open_commitment_context(
        session,
        project_id=project.id,
        pinned_commitment_ids=pinned_commitment_ids,
        # Lets the lookup rank by what this message is actually about, rather
        # than handing over the twelve newest and hoping. Guarded above:
        # message.text is non-empty by this point.
        message=message.text,
    )
    case = build_case(
        message,
        channel_type=channel.type,
        party_display_name=party_name or message.sender_external_id,
        channel_capability=capability,
    )

    created = 0
    try:
        async with session.begin_nested():
            outcome = await extract_case(
                session,
                project_id=project.id,
                organisation_id=project.organisation_id,
                context=context,
                case=case,
                client=client,
                ledger_context=ledger_context,
            )
            created = len(outcome.created)
            await _finalise_message_evidence(
                session,
                message=message,
                evidence_ids=outcome.evidence_ids,
                storage=storage or get_storage_backend(),
            )
            if outcome.linked:
                # Not a no-op run: the message was read as being *about*
                # commitments already on the ledger, and each of those gained
                # an Evidence row rather than the ledger gaining a duplicate.
                logger.info(
                    "message %s linked to %d already-logged commitment(s): %s",
                    message.id, len(outcome.linked), [str(c) for c in outcome.linked],
                )
    except RejectedExtraction as e:
        created = 0
        logger.info("extraction rejected for message %s: %s", message.id, e)
    finally:
        message.extraction_attempted_at = datetime.now(dt_timezone.utc)
        await session.flush()
    return created


async def ingest_raw_message(
    session: AsyncSession,
    *,
    project: Project,
    channel: Channel,
    adapter: ChannelAdapter,
    raw: RawCapturedMessage,
    client: ModelClient | None = None,
    storage: StorageBackend | None = None,
) -> tuple[Message | None, bool, int, int]:
    """One message through the full item-3 pipeline: normalise -> identity
    resolve -> consent gate -> media (item 6) -> extract. Returns (message,
    is_new, commitments_created, media_processed) — `message` is the
    durable row (existing, on a duplicate; None, on an opt-out); media and
    extraction only ever run for a message this call itself just inserted,
    since both downstream steps guard against reprocessing an
    already-handled row anyway (extract_from_message's own
    `extraction_attempted_at`; process_message_media's own `storage_key`
    check), but skipping both entirely for a known-duplicate avoids the
    extra queries.
    """
    result = await normalise_and_ingest(session, project=project, channel=channel, raw=raw)
    if result.message is None or not result.is_new:
        return result.message, result.is_new, 0, 0

    # FR-WBK-06/07: rides this same pipeline, not a second inbound path —
    # checked before extraction, on every newly-captured message, so a reply
    # to a pending write-back is matched (transitioned or escalated) even if
    # it also happens to contain language extract_from_message would try to
    # read as a new commitment. Deliberately does not forward `client`
    # (extract_case's extraction-role override) — reply parsing is a
    # reasoning-role call (app/writeback/reply.py's own get_client("reasoning")
    # default), a different model role than extraction, same distinction
    # app/llm/factory.py's Role type draws everywhere else in this codebase.
    reply = await handle_potential_reply(
        session, project=project, channel_id=channel.id, message=result.message
    )

    media_processed = await process_pending_media_for_message(
        session,
        project=project,
        channel=channel,
        adapter=adapter,
        message=result.message,
        storage=storage or get_storage_backend(),
    )
    # A reply that just transitioned a commitment is *about* that commitment.
    # Extraction still runs — a vendor can answer the question and add a new
    # promise in the same breath ("yes confirmed, and the frame slips 2 days")
    # so skipping it outright would lose real commitments — but the matched
    # commitment is pinned into the already-logged context it is shown, so the
    # part of the reply that answers the question links to the existing row
    # instead of being read as a second, duplicate promise.
    pinned = (reply.commitment_id,) if reply.commitment_id is not None else ()
    created = await extract_from_message(
        session, project=project, channel=channel, message=result.message, client=client,
        storage=storage, pinned_commitment_ids=pinned,
    )
    return result.message, True, created, media_processed


async def ingest_channel_backlog(
    session: AsyncSession,
    *,
    project: Project,
    channel: Channel,
    adapter: ChannelAdapter,
    since: datetime | None = None,
    client: ModelClient | None = None,
    storage: StorageBackend | None = None,
) -> IngestionSummary:
    """FR-CAP-08's backfill path and item 11's scheduled-window trigger both
    call this. Processes `adapter.fetch_backlog`'s messages **sequentially,
    in the order the adapter yields them** — the "per-channel ordered" half
    of item 3's own queue requirement (app/capture/worker.py's own docstring
    covers the other half: at most one ingestion job in flight per channel
    at a time). Commits after every message, not once at the end, so a
    crash partway through a large backlog leaves every already-processed
    message durably captured (NFR-AVL-02) and resumable from
    `summary.latest_sent_at` rather than re-fetching the whole backlog from
    `since` again.

    Each message's own `org_scoped_transaction` (app/core/db.py) re-asserts
    RLS's `app.current_org_id` immediately before that message's own commit
    — not once for the whole backlog. A per-message commit here is exactly
    the multi-commit-per-session shape that context manager's own docstring
    warns about: this loop is what a real, live run of `ingest_channel_job`
    hit the bug against (a second message in the same backlog failing RLS
    because a concurrent arq cron job reused the connection between this
    message's commit and the next), before this was fixed.
    """
    summary = IngestionSummary()
    async for raw in adapter.fetch_backlog(channel, since=since):
        summary.received += 1
        async with org_scoped_transaction(session, project.organisation_id):
            message, is_new, created, media_processed = await ingest_raw_message(
                session, project=project, channel=channel, adapter=adapter, raw=raw, client=client,
                storage=storage,
            )

        if message is None:
            summary.opted_out += 1
            continue
        if is_new:
            summary.new_messages += 1
        else:
            summary.duplicates += 1
        summary.commitments_created += created
        summary.media_processed += media_processed
        if summary.latest_sent_at is None or message.sent_at > summary.latest_sent_at:
            summary.latest_sent_at = message.sent_at

    return summary
