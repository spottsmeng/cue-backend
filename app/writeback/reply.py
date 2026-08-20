import json
import logging
import uuid
from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.capture.models import Message
from app.foresight.notification import create_notification, default_recipients
from app.ledger.audit import record_audit_event
from app.ledger.lifecycle import InvalidTransition, validate_transition
from app.llm.client import ModelClient
from app.llm.cost import record_llm_usage
from app.llm.factory import get_client
from app.models import Commitment, Project
from app.parties.service import recompute_vendor_metrics
from app.twin.service import recompute_on_commitment_transition
from app.writeback.models import OutboundMessage
from app.writeback.schema import REPLY_PARSE_JSON_SCHEMA, ReplyParse

logger = logging.getLogger("cue.writeback.reply")

# FR-WBK-06/07 (PRD §6.12): "Parse the reply into a structured commitment
# state transition, in either language" / "Fall back gracefully to plain
# text if a reply cannot be parsed; escalate to the PM rather than guess."
# Rides app/capture/pipeline.py's existing ingestion pipeline (item 3, real
# capture) rather than a second inbound path — this module's one entry point,
# handle_potential_reply, is called from ingest_raw_message right after a new
# Message is durably captured, before extraction runs.

_PROMPT_TEMPLATE = """A vendor was sent this closed question, in {language}: "{question}"

They replied: "{reply}"

Does this reply clearly answer the question, and if so, what does it imply about the commitment's current state? The commitment was in state {current_state!r} when the question was sent. Respond with parseable=true and the resulting to_state only if the reply is an unambiguous answer; otherwise parseable=false."""


@dataclass
class ReplyHandlingResult:
    matched_outbound: bool
    outcome: str | None = None  # "transitioned" | "escalated" | None (no match)
    # Which commitment the reply was about, when one was matched. The caller
    # (app/capture/pipeline.py) feeds this into extraction's already-logged
    # context so the very next step does not read the same reply a second
    # time as a brand-new promise — the message has, by construction, just
    # been shown to be about this existing commitment.
    commitment_id: uuid.UUID | None = None


async def _find_pending_outbound(
    session: AsyncSession, *, channel_id: uuid.UUID, message: Message
) -> OutboundMessage | None:
    """The most recent `sent`, not-yet-replied-to OutboundMessage on this
    channel, sent before this inbound message arrived — FR-WBK-04's own "at
    most one message per group per day" ceiling means there is normally at
    most one candidate at all; ordering by sent_at desc and taking the first
    is a defensive tie-break, not load-bearing."""
    return (
        await session.execute(
            select(OutboundMessage)
            .where(
                OutboundMessage.channel_id == channel_id,
                OutboundMessage.status == "sent",
                OutboundMessage.reply_message_id.is_(None),
                OutboundMessage.sent_at <= message.sent_at,
            )
            .order_by(OutboundMessage.sent_at.desc())
        )
    ).scalars().first()


async def _parse_reply(
    outbound: OutboundMessage,
    message: Message,
    *,
    current_state: str,
    client: ModelClient,
    session: AsyncSession | None = None,
    project: Project | None = None,
) -> ReplyParse:
    prompt = _PROMPT_TEMPLATE.format(
        language=outbound.language,
        question=outbound.draft_text,
        reply=message.text or "",
        current_state=current_state,
    )
    try:
        raw, usage = await client.complete(prompt, REPLY_PARSE_JSON_SCHEMA)
        if session is not None and project is not None:
            await record_llm_usage(
                session, organisation_id=project.organisation_id, project_id=project.id,
                role="reasoning", purpose="writeback_reply_parse", usage=usage,
            )
        return ReplyParse.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError) as e:
        logger.info("reply parse failed schema validation for outbound %s: %s", outbound.id, e)
        return ReplyParse(parseable=False, to_state=None, reasoning=f"model output invalid: {e}")


async def _escalate(
    session: AsyncSession,
    *,
    project: Project,
    outbound: OutboundMessage,
    commitment: Commitment,
    message: Message,
    reason: str,
) -> None:
    """FR-WBK-07's fallback: never guess, escalate to the owning PM via the
    Foresight session's own notification path (app/foresight/notification.py's
    default_recipients — "every user holding project_manager membership,
    falling back to administrator if the project has none"), same helper
    app/foresight/escalation.py already uses for "the owning PM"."""
    outbound.reply_message_id = message.id
    outbound.reply_outcome = "escalated"
    outbound.reply_detail = {"reason": reason, "reply_text": message.text}
    await session.flush()

    recipients = await default_recipients(session, project.id)
    for recipient_id in recipients:
        await create_notification(
            session,
            project=project,
            recipient_id=recipient_id,
            severity="medium",
            downstream_consequence=(
                f"Vendor reply on {commitment.deliverable_en!r} could not be applied "
                f"automatically ({reason}) — needs manual review."
            ),
            commitment_id=commitment.id,
            detail={
                "event_type": "writeback_reply_escalation",
                "outbound_message_id": str(outbound.id),
                "reply_message_id": str(message.id),
                "reason": reason,
            },
        )


async def handle_potential_reply(
    session: AsyncSession,
    *,
    project: Project,
    channel_id: uuid.UUID,
    message: Message,
    client: ModelClient | None = None,
) -> ReplyHandlingResult:
    """Called from app/capture/pipeline.py's ingest_raw_message for every
    newly-captured message. Returns matched_outbound=False immediately (the
    common case — most messages are not replies to a pending write-back) for
    a channel with no pending OutboundMessage, without ever calling the LLM.
    Does not commit — caller (the pipeline) owns the transaction boundary,
    same as every other step in that pipeline.
    """
    outbound = await _find_pending_outbound(session, channel_id=channel_id, message=message)
    if outbound is None:
        return ReplyHandlingResult(matched_outbound=False)

    commitment = (
        await session.execute(select(Commitment).where(Commitment.id == outbound.commitment_id))
    ).scalar_one_or_none()
    if commitment is None:
        # The commitment this write-back concerned no longer exists — nothing
        # to transition or escalate against; leave the reply unmatched so a
        # later, genuinely pending outbound (if any) can still claim it.
        return ReplyHandlingResult(matched_outbound=False)

    client = client or get_client("reasoning")
    parsed = await _parse_reply(
        outbound, message, current_state=commitment.state, client=client,
        session=session, project=project,
    )

    if not parsed.parseable or not parsed.to_state:
        await _escalate(
            session, project=project, outbound=outbound, commitment=commitment, message=message,
            reason=f"reply not parseable: {parsed.reasoning}" if parsed.reasoning else "reply not parseable",
        )
        return ReplyHandlingResult(
            matched_outbound=True, outcome="escalated", commitment_id=commitment.id
        )

    from_state = commitment.state
    try:
        validate_transition(from_state, parsed.to_state)
    except InvalidTransition as e:
        await _escalate(
            session, project=project, outbound=outbound, commitment=commitment, message=message,
            reason=f"reply implied an invalid transition {from_state!r} -> {parsed.to_state!r}: {e}",
        )
        return ReplyHandlingResult(
            matched_outbound=True, outcome="escalated", commitment_id=commitment.id
        )

    commitment.state = parsed.to_state
    await session.flush()  # the DB trigger checks this too, same defense-in-depth as everywhere else

    outbound.reply_message_id = message.id
    outbound.reply_outcome = "transitioned"
    outbound.reply_detail = {"to_state": parsed.to_state, "reasoning": parsed.reasoning}
    await session.flush()

    await record_audit_event(
        session,
        project_id=project.id,
        commitment_id=commitment.id,
        action="state_transition",
        actor_id=None,
        from_state=from_state,
        to_state=parsed.to_state,
        detail={"reason": "vendor reply", "outbound_message_id": str(outbound.id)},
    )
    await recompute_on_commitment_transition(
        session, project_id=project.id, commitment=commitment, to_state=parsed.to_state, actor_id=None,
    )
    await recompute_vendor_metrics(session, commitment.party_id)

    return ReplyHandlingResult(
        matched_outbound=True, outcome="transitioned", commitment_id=commitment.id
    )
