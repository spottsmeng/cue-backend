"""FR-CAP-06/07 (PRD §6.1): "Post a one-time bilingual consent notice on
joining any external group, and record acceptance, objection and opt-out to
the consent ledger" / "Honour opt-out immediately: cease capture for the
opting-out participant."

`upsert_consent_record` is extracted from app/api/consent.py's own
consent_action_request (the Governance session, Prompt 5) rather than
duplicated — that endpoint is a human Administrator recording an outcome;
this module is a ChannelAdapter recording the same kind of outcome
automatically. Both need the identical upsert-by-(party_id, project_id)
semantics, so there is exactly one implementation, not two that could drift.
"""

import uuid
from datetime import datetime, timezone as dt_timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.capture.adapters.registry import get_adapter
from app.models import Channel, ConsentRecord, Party

# FR-CAP-06's own wording: "bilingual". Not project/channel-specific
# copy — a generic notice naming CUE as the automated capture agent, since
# the party being notified has no relationship with this text beyond "a
# system is now logging this conversation for project recordkeeping."
BILINGUAL_CONSENT_NOTICE = (
    "This conversation is being recorded by CUE, an automated project "
    "recordkeeping system, on behalf of Pico. Messages, media and voice "
    "notes you send here may be captured, transcribed and stored as part "
    "of the project record. Reply ACCEPT to continue, OBJECT to raise a "
    "concern, or OPT-OUT to be excluded from capture.\n"
    "本对话正由 CUE（Pico 委托的自动化项目记录系统）记录。您在此发送的文字、"
    "媒体及语音信息可能被采集、转写并存档,作为项目记录的一部分。"
    "回复 ACCEPT 表示同意,OBJECT 表示提出异议,OPT-OUT 表示要求不被记录。"
)


async def upsert_consent_record(
    session: AsyncSession,
    *,
    party_id: uuid.UUID,
    project_id: uuid.UUID,
    status: str,
    evidence: str | None = None,
    notice_sent_at: datetime | None = None,
) -> ConsentRecord:
    existing = (
        await session.execute(
            select(ConsentRecord).where(
                ConsentRecord.party_id == party_id, ConsentRecord.project_id == project_id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.status = status
        existing.evidence = evidence
        if notice_sent_at is not None:
            existing.notice_sent_at = notice_sent_at
        await session.flush()
        # updated_at has a server-side onupdate=func.now() — same
        # MissingGreenlet trap app/api/commitments.py's _refresh_updated_at
        # documents.
        await session.refresh(existing, attribute_names=["updated_at"])
        return existing

    record = ConsentRecord(
        party_id=party_id, project_id=project_id, notice_sent_at=notice_sent_at,
        status=status, evidence=evidence,
    )
    session.add(record)
    await session.flush()
    return record


async def post_consent_notice(
    session: AsyncSession, *, channel: Channel, party: Party, to_external_id: str
) -> ConsentRecord:
    """FR-CAP-06's "one-time" — the caller (item 3's pipeline, on first
    seeing a new party on a channel) is what makes this one-time, by only
    calling it when app/capture/identity.py's resolve_identity reports
    `created_new_identity=True`, not by any check in here.

    Raises NotImplementedError, uncaught, for a channel whose capability has
    no send() concept (file_storage — Nextcloud/SharePoint) — the same
    "existing, correct behaviour, not a gap to fill" ChannelAdapter.send()
    already documents. Consent has no meaning for a channel with no
    conversation to consent to (a drive-delta document event has no message
    sender to notify); the caller is expected to simply not invoke this for
    a file_storage channel, the same way it already can't invoke send() for
    one for any other reason.
    """
    adapter = get_adapter(channel.type)
    await adapter.send(channel, to_external_id, BILINGUAL_CONSENT_NOTICE)
    return await upsert_consent_record(
        session,
        party_id=party.id,
        project_id=channel.project_id,
        status="pending",
        evidence=f"bilingual consent notice sent via channel_type={channel.type!r}",
        notice_sent_at=datetime.now(dt_timezone.utc),
    )


async def is_opted_out(session: AsyncSession, *, party_id: uuid.UUID, project_id: uuid.UUID) -> bool:
    """FR-CAP-07's gate — checked by the normaliser (item 3) before a
    Message row is ever written for this (party, project). Redacting prior
    content per retention policy (FR-CAP-07's other half) is deliberately
    not implemented here: deletion/redaction automation was explicitly out
    of scope for the Governance session that built RetentionPolicy (Prompt
    5) and remains so — RetentionPolicy is configuration a future job would
    read, not a resolver anything currently calls (that table's own
    docstring already states this limitation; this module doesn't change
    it). What this function *does* guarantee is FR-CAP-07's other, more
    urgent half: capture stops immediately, not eventually.
    """
    status = (
        await session.execute(
            select(ConsentRecord.status).where(
                ConsentRecord.party_id == party_id, ConsentRecord.project_id == project_id
            )
        )
    ).scalar_one_or_none()
    return status == "opted_out"
