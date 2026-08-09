import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.capture.models import Message
from app.capture.normalise import detect_language

# FR-WBK-02: "Compose in the vendor's detected language, defaulting to the
# language of that group's prevailing traffic." Reuses the real-capture
# session's own language detection (app/capture/normalise.py's
# detect_language) rather than building a second mechanism, per Prompt 12's
# own explicit instruction — "the group's prevailing traffic" is answered by
# running that same script-ratio heuristic over a window of the group's own
# recent messages (any author) concatenated together, not just the one
# vendor's, since detect_language already knows how to report a genuinely
# code-switched result across that combined text.
_TRAFFIC_WINDOW = 20


async def resolve_channel_language(
    session: AsyncSession, *, channel_id: uuid.UUID, fallback: str
) -> str:
    """`fallback` is the commitment's own founding Evidence.language — used
    only when this channel has no real-capture message history yet (a
    fixture-derived or manually-entered commitment, or a channel captured
    before Prompt 11's real-capture session ran), so drafting can never
    silently default to English regardless of the message that established
    the commitment in the first place."""
    rows = (
        await session.execute(
            select(Message.text)
            .where(Message.channel_id == channel_id, Message.text.is_not(None))
            .order_by(Message.sent_at.desc())
            .limit(_TRAFFIC_WINDOW)
        )
    ).scalars().all()
    if not rows:
        return fallback

    detected = detect_language("\n".join(rows))
    return detected if detected != "und" else fallback
