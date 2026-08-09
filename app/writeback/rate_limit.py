import uuid
from datetime import datetime, timezone as dt_timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Channel, Project
from app.writeback.models import OutboundMessage

# FR-WBK-04: "Rate-limit outbound messages per group (default one per day,
# configurable, hard ceiling enforced)." CLAUDE.md's non-obvious note:
# "no code path (including a retry, including an admin override attempt) can
# exceed it without an explicit, audited configuration change to the ceiling
# itself" — a plain SELECT COUNT(*) followed by an INSERT in two separate
# round trips would let two concurrent `send` calls both read "0 sent today"
# before either commits. Instead: `SELECT ... FOR UPDATE` on the *Channel*
# row itself (not on any OutboundMessage row — there may be zero, or several
# already-authorised-but-unsent ones, none of which is a natural lock
# target) serialises every send attempt for that channel within one
# transaction. The second concurrent caller blocks until the first commits
# or rolls back, then re-counts and sees the now-true state — a real
# transactional check-then-insert, not an application-level counter that
# could drift under concurrency.


class RateCeilingExceeded(Exception):
    """Raised when a group's configured daily ceiling is already met. Never
    caught and retried automatically — FR-WBK-04's ceiling is a hard stop,
    not a queue-and-retry-later mechanism."""


def _local_date_bucket(channel_id: uuid.UUID, project: Project, *, now: datetime) -> str:
    local_date = now.astimezone(ZoneInfo(project.timezone)).date()
    return f"{channel_id}:{local_date.isoformat()}"


async def reserve_send_slot(
    session: AsyncSession, *, project: Project, channel_id: uuid.UUID, now: datetime | None = None
) -> str:
    """Locks the channel, counts today's already-`sent` OutboundMessage rows
    against `project.writeback_daily_ceiling`, and returns the bucket key the
    caller must stamp onto the row it is about to mark `sent` — inside the
    *same* transaction this lock was taken in, before commit, so the lock is
    still held while the new row is inserted. Raises RateCeilingExceeded
    without releasing anything itself; the caller's rollback/commit does
    that, same as every other exception path in this codebase.
    """
    now = now or datetime.now(dt_timezone.utc)

    await session.execute(
        select(Channel.id).where(Channel.id == channel_id).with_for_update()
    )

    bucket = _local_date_bucket(channel_id, project, now=now)
    sent_today = (
        await session.execute(
            select(func.count())
            .select_from(OutboundMessage)
            .where(
                OutboundMessage.channel_id == channel_id,
                OutboundMessage.rate_limit_bucket == bucket,
                OutboundMessage.status == "sent",
            )
        )
    ).scalar_one()

    if sent_today >= project.writeback_daily_ceiling:
        raise RateCeilingExceeded(
            f"channel {channel_id} has already sent {sent_today} message(s) today "
            f"(ceiling: {project.writeback_daily_ceiling})"
        )
    return bucket
