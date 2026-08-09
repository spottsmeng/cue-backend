import uuid
from datetime import datetime, timezone as dt_timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.capture.adapters.registry import get_adapter
from app.capture.models import Message
from app.ledger.audit import record_audit_event
from app.llm.client import ModelClient
from app.models import Channel, ChannelIdentity, Commitment, Evidence, Project
from app.writeback.compose import compose_draft
from app.writeback.language import resolve_channel_language
from app.writeback.models import OutboundMessage
from app.writeback.rate_limit import reserve_send_slot

# Orchestrates FR-WBK-01 through 05/08's draft -> authorise -> send cycle —
# the one path app/api/writeback.py's three endpoints call, so the API layer
# stays a thin translation of HTTP verbs onto this module rather than
# re-implementing any of the sequencing itself.


class WritebackTargetUnresolved(Exception):
    """Raised at draft time when this commitment has no way to identify
    "the originating vendor group" (FR-WBK-01) — no real-capture evidence
    (a fixture-derived or manually-entered commitment), or no
    ChannelIdentity linking the vendor party to that channel's type. Not a
    guess-and-send situation: this session builds entirely on M8's real
    capture data, per Prompt 12's own EXPLICITLY OUT OF SCOPE section."""


class InvalidWritebackTransition(Exception):
    """Raised when authorise/send is called on an OutboundMessage not in the
    state that action requires — FR-WBK-05's structural separation, enforced
    here as well as by the DB CHECK constraint on the row itself."""


async def _resolve_writeback_target(
    session: AsyncSession, *, commitment: Commitment
) -> tuple[Channel, str, str]:
    """Returns (channel, to_external_id, fallback_language) — FR-WBK-01's
    "originating vendor group" is the channel of the real captured message
    that established this commitment (via its Evidence row's message_id,
    app/capture/pipeline.py's _finalise_message_evidence), and the vendor's
    reachable identity on that channel type."""
    evidence = (
        await session.execute(
            select(Evidence)
            .where(Evidence.commitment_id == commitment.id, Evidence.message_id.is_not(None))
            .order_by(Evidence.sent_at.desc())
        )
    ).scalars().first()
    if evidence is None or evidence.message_id is None:
        raise WritebackTargetUnresolved(
            f"commitment {commitment.id} has no real-capture evidence — cannot identify "
            "the originating vendor group (FR-WBK-01)"
        )

    message = (
        await session.execute(select(Message).where(Message.id == evidence.message_id))
    ).scalar_one_or_none()
    if message is None:
        raise WritebackTargetUnresolved(f"evidence {evidence.id}'s message no longer exists")

    channel = (
        await session.execute(select(Channel).where(Channel.id == message.channel_id))
    ).scalar_one_or_none()
    if channel is None:
        raise WritebackTargetUnresolved(f"message {message.id}'s channel no longer exists")

    identity = (
        await session.execute(
            select(ChannelIdentity.external_id)
            .where(
                ChannelIdentity.party_id == commitment.party_id,
                ChannelIdentity.channel_type == channel.type,
            )
            .order_by(ChannelIdentity.updated_at.desc())
        )
    ).scalars().first()
    if identity is None:
        raise WritebackTargetUnresolved(
            f"party {commitment.party_id} has no known identity on channel_type={channel.type!r}"
        )

    return channel, identity, evidence.language


async def draft_writeback(
    session: AsyncSession,
    *,
    project: Project,
    commitment: Commitment,
    client: ModelClient | None = None,
) -> OutboundMessage:
    """FR-WBK-01/02/03: resolves the originating group, the vendor's
    detected language, and composes a single closed question — writes a
    `draft` row, nothing more. No audit event: per Prompt 12 item 6, only a
    real send is logged to the commitment's audit trail."""
    channel, to_external_id, fallback_language = await _resolve_writeback_target(
        session, commitment=commitment
    )
    language = await resolve_channel_language(
        session, channel_id=channel.id, fallback=fallback_language
    )
    draft_text = await compose_draft(
        commitment, language=language, client=client,
        session=session, organisation_id=project.organisation_id,
    )

    outbound = OutboundMessage(
        project_id=project.id,
        channel_id=channel.id,
        commitment_id=commitment.id,
        party_id=commitment.party_id,
        to_external_id=to_external_id,
        language=language,
        draft_text=draft_text,
        status="draft",
    )
    session.add(outbound)
    await session.flush()
    return outbound


async def edit_writeback_draft(
    session: AsyncSession, *, outbound: OutboundMessage, draft_text: str
) -> OutboundMessage:
    """FR-WBK-05's "review/edit before authorisation" — edit half of that,
    added alongside draft/authorise/send rather than folded into any of
    them, same one-call-one-purpose shape the three-tap precedent already
    established. Only a `draft` row is editable; once authorised, the text a
    human signed off on is frozen (InvalidWritebackTransition otherwise, the
    same guard authorise_writeback uses). No audit event here — only a real
    `send` is logged to the shared trail (this module's own docstring,
    Prompt 12 item 6); an edit is fully visible via this row's own
    `updated_at` and never itself the decision being audited."""
    if outbound.status != "draft":
        raise InvalidWritebackTransition(
            f"cannot edit an OutboundMessage in status {outbound.status!r} — only a draft"
        )
    outbound.draft_text = draft_text
    await session.flush()
    await session.refresh(outbound, attribute_names=["updated_at"])
    return outbound


async def authorise_writeback(
    session: AsyncSession, *, outbound: OutboundMessage, actor_id: uuid.UUID
) -> OutboundMessage:
    """FR-WBK-05's required, separate human step. Records who — never
    inferred, never defaulted."""
    if outbound.status != "draft":
        raise InvalidWritebackTransition(
            f"cannot authorise an OutboundMessage in status {outbound.status!r} — only a draft"
        )
    outbound.authorised_by = actor_id
    outbound.authorised_at = datetime.now(dt_timezone.utc)
    outbound.status = "authorised"
    await session.flush()
    # updated_at has a server-side onupdate=func.now() — after an UPDATE
    # flush, SQLAlchemy expires it rather than fetching it eagerly, so a
    # bare attribute access later (e.g. response serialization) would
    # silently try a lazy-load outside the async greenlet bridge and raise
    # MissingGreenlet. Same fix app/api/commitments.py's _refresh_updated_at
    # already applies.
    await session.refresh(outbound, attribute_names=["updated_at"])
    return outbound


async def send_writeback(
    session: AsyncSession, *, project: Project, outbound: OutboundMessage, actor_id: uuid.UUID
) -> OutboundMessage:
    """FR-WBK-04/08: only callable on an already-authorised draft
    (InvalidWritebackTransition otherwise — the DB CHECK constraint is the
    second, defense-in-depth layer). Reserves the rate-limit slot *before*
    calling the adapter, inside this same transaction, so the ceiling check
    and the send are atomic with respect to concurrent callers
    (app/writeback/rate_limit.py's own docstring); the row is only marked
    `sent` after the adapter call actually succeeds, so a failed send never
    consumes a slot or gets logged as delivered.
    """
    if outbound.status != "authorised":
        raise InvalidWritebackTransition(
            f"cannot send an OutboundMessage in status {outbound.status!r} — only an authorised draft"
        )

    bucket = await reserve_send_slot(session, project=project, channel_id=outbound.channel_id)

    channel = (
        await session.execute(select(Channel).where(Channel.id == outbound.channel_id))
    ).scalar_one()
    adapter = get_adapter(channel.type)
    await adapter.send(channel, outbound.to_external_id, outbound.draft_text)

    outbound.status = "sent"
    outbound.sent_at = datetime.now(dt_timezone.utc)
    outbound.rate_limit_bucket = bucket
    await session.flush()
    await session.refresh(outbound, attribute_names=["updated_at"])  # see authorise_writeback's own comment

    await record_audit_event(
        session,
        project_id=project.id,
        commitment_id=outbound.commitment_id,
        action="outbound_sent",
        actor_id=actor_id,
        detail={
            "outbound_message_id": str(outbound.id),
            "channel_id": str(channel.id),
            "language": outbound.language,
        },
    )
    return outbound
