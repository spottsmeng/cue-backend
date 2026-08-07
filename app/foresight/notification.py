import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.foresight.audit import record_foresight_audit_event
from app.foresight.models import Deviation, Notification, QuietHoursConfig, Risk, WebhookSubscription
from app.identity.models import Membership
from app.models import Commitment, Project

# PRD §6.15 (FR-NTF): the notification core every detector and the
# escalation sweep surface through. FR-NTF-02's "never a bare event"
# enforced-column discipline lives on Notification itself
# (app/foresight/models.py); this module is what computes FR-NTF-03's
# collapsing and FR-NTF-04's quiet-hours eligibility at creation time, plus
# the one real delivery adapter this session builds (webhook — see
# WebhookSubscription's own docstring for why that one isn't deferred the
# way push/email/Teams are).

_SEVERITY_ORDER = ["low", "medium", "high", "critical"]


async def _resolve_quiet_hours(session: AsyncSession, project: Project) -> QuietHoursConfig | None:
    stmt = select(QuietHoursConfig).where(QuietHoursConfig.project_id == project.id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def compute_deliverable_at(
    session: AsyncSession, project: Project, *, severity: str, now: datetime | None = None
) -> datetime:
    """FR-NTF-04: "Respect quiet hours, with an override for critical items
    during live-event windows." Quiet hours are configured per-project
    (app/foresight/models.py's QuietHoursConfig docstring documents that
    choice over per-user) — local to Project.timezone. The "live-event
    window" reuses Project.event_start/event_end directly rather than a
    separate declared-window column: that pair already *is* this project's
    own declared live-event window.

    No QuietHoursConfig row for this project at all -> always eligible now
    (nothing configured means nothing to respect yet, same "config, not a
    resolver" posture RetentionPolicy's own docstring establishes for an
    unset axis).
    """
    now = now or datetime.now(dt_timezone.utc)
    config = await _resolve_quiet_hours(session, project)
    if config is None:
        return now

    if (
        project.event_start is not None
        and project.event_end is not None
        and project.event_start <= now <= project.event_end
        and _SEVERITY_ORDER.index(severity) >= _SEVERITY_ORDER.index(config.critical_severity_threshold)
    ):
        return now  # FR-NTF-04's override

    local_now = now.astimezone(ZoneInfo(project.timezone))
    local_time = local_now.time()
    start, end = config.quiet_start_local, config.quiet_end_local
    in_quiet = (start <= local_time < end) if start < end else (local_time >= start or local_time < end)
    if not in_quiet:
        return now

    quiet_ends_local = local_now.replace(
        hour=end.hour, minute=end.minute, second=end.second, microsecond=0
    )
    if quiet_ends_local <= local_now:
        quiet_ends_local += timedelta(days=1)
    return quiet_ends_local.astimezone(dt_timezone.utc)


async def create_notification(
    session: AsyncSession,
    *,
    project: Project,
    recipient_id: uuid.UUID,
    severity: str,
    downstream_consequence: str,
    risk: Risk | None = None,
    deviation: Deviation | None = None,
    commitment_id: uuid.UUID | None = None,
    detail: dict | None = None,
) -> Notification:
    """FR-NTF-03: collapses into this recipient's existing pending
    (not yet sent, not yet acknowledged) notification for this project
    rather than firing a second one — a distinct dedup from FR-FOR-10's
    Risk-level check (app/foresight/risk.py's own docstring): that one
    prevents duplicate *Risk* rows for one condition; this one merges
    distinct, already-deduplicated Risks/Deviations that arrive close
    together for the same recipient into one notification. The collapsed
    notification's severity/consequence escalate to the more urgent of the
    two — a recipient should never see a *less* urgent summary than any one
    of the things collapsed into it.
    """
    existing_candidates = (
        await session.execute(
            select(Notification).where(
                Notification.project_id == project.id,
                Notification.recipient_id == recipient_id,
                Notification.sent_at.is_(None),
                Notification.acknowledged_at.is_(None),
            )
        )
    ).scalars().all()
    existing = max(existing_candidates, key=lambda n: n.created_at) if existing_candidates else None

    if existing is not None:
        if risk is not None and risk.id != existing.risk_id and risk.id not in existing.collapsed_risk_ids:
            existing.collapsed_risk_ids = [*existing.collapsed_risk_ids, risk.id]
        existing.collapsed_count += 1
        if _SEVERITY_ORDER.index(severity) > _SEVERITY_ORDER.index(existing.severity):
            existing.severity = severity
            existing.downstream_consequence = downstream_consequence
        await session.flush()
        return existing

    deliverable_at = await compute_deliverable_at(session, project, severity=severity)
    notification = Notification(
        project_id=project.id,
        recipient_id=recipient_id,
        risk_id=risk.id if risk is not None else None,
        deviation_id=deviation.id if deviation is not None else None,
        commitment_id=commitment_id,
        severity=severity,
        downstream_consequence=downstream_consequence,
        deliverable_at=deliverable_at,
        detail=detail or {},
    )
    session.add(notification)
    await session.flush()

    await record_foresight_audit_event(
        session, project_id=project.id, action="notification_created", actor_id=None,
        notification_id=notification.id,
        risk_id=risk.id if risk is not None else None,
        deviation_id=deviation.id if deviation is not None else None,
        detail={},
    )
    return notification


async def default_recipients(session: AsyncSession, project_id: uuid.UUID) -> set[uuid.UUID]:
    """FR-LCY-05's "the owning PM" — every user holding `project_manager`
    membership on this project, falling back to `administrator` if the
    project genuinely has no PM assigned yet (never silently notifying no
    one)."""
    pm_ids = set(
        (
            await session.execute(
                select(Membership.user_id).where(
                    Membership.project_id == project_id, Membership.role == "project_manager"
                )
            )
        ).scalars().all()
    )
    if pm_ids:
        return pm_ids
    return set(
        (
            await session.execute(
                select(Membership.user_id).where(
                    Membership.project_id == project_id, Membership.role == "administrator"
                )
            )
        ).scalars().all()
    )


async def dispatch_event(
    session: AsyncSession,
    *,
    project: Project,
    event_type: str,
    risk: Risk | None = None,
    deviation: Deviation | None = None,
    commitment: Commitment | None = None,
    recipient_ids: set[uuid.UUID] | None = None,
) -> list[Notification]:
    """The single event-dispatch entry point for commitment/risk/deviation
    events (item 8) — every detector, the lifecycle sweep and the
    escalation sweep call this rather than constructing Notification rows
    directly. `recipient_ids` defaults to default_recipients above when not
    given explicitly (escalation.py passes an explicit set — the role
    holders it just escalated to)."""
    if risk is not None:
        severity, consequence = risk.severity, risk.downstream_consequence
    elif deviation is not None:
        severity = "medium"
        consequence = f"Deviation drafted: {deviation.description_en}"
    elif commitment is not None:
        severity = {"broken": "high", "at_risk": "medium"}.get(commitment.state, "low")
        consequence = f"Commitment {commitment.deliverable_en!r} transitioned to {commitment.state!r}."
    else:
        raise ValueError("dispatch_event needs at least one of risk, deviation or commitment")

    if recipient_ids is None:
        recipient_ids = await default_recipients(session, project.id)

    return [
        await create_notification(
            session, project=project, recipient_id=recipient_id, severity=severity,
            downstream_consequence=consequence, risk=risk, deviation=deviation,
            commitment_id=commitment.id if commitment is not None else None,
            detail={"event_type": event_type},
        )
        for recipient_id in recipient_ids
    ]


def _event_type_for_notification(notification: Notification) -> str:
    if notification.risk_id is not None:
        return "risk"
    if notification.deviation_id is not None:
        return "deviation"
    return "commitment"


async def deliver_webhook(subscription: WebhookSubscription, payload: dict) -> bool:
    """The only real delivery adapter this session builds (WebhookSubscription's
    own docstring). HMAC-SHA256 over the raw request body, in an
    `X-CUE-Signature` header, so a receiver can verify a delivery genuinely
    came from CUE — never trusted on the receiving end without it, same
    "verified, not trusted" posture as everywhere else in this codebase.
    Returns False (never raises) on any transport/HTTP failure — a delivery
    attempt failing is an ordinary, expected outcome the next sweep retries,
    not an exceptional one."""
    body = json.dumps(payload, sort_keys=True).encode()
    signature = hmac.new(subscription.secret.encode(), body, hashlib.sha256).hexdigest()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                subscription.url,
                content=body,
                headers={"content-type": "application/json", "x-cue-signature": signature},
            )
            response.raise_for_status()
        return True
    except httpx.HTTPError:
        return False


async def deliver_due_notifications(session: AsyncSession, project: Project) -> list[Notification]:
    """The arq-scheduled fan-out job's own work (CUE-Tech-Stack.md §2.4:
    "notification fan-out" is arq's named use case) — every Notification
    past its own `deliverable_at` (FR-NTF-04) that hasn't been sent yet,
    delivered to every active WebhookSubscription subscribed to its event
    type. A notification with no matching subscription stays pending
    (correct — nothing is listening, not an error) rather than being marked
    sent with nowhere having actually received it."""
    now = datetime.now(dt_timezone.utc)
    due = (
        await session.execute(
            select(Notification).where(
                Notification.project_id == project.id,
                Notification.sent_at.is_(None),
                Notification.deliverable_at <= now,
            )
        )
    ).scalars().all()
    if not due:
        return []

    subscriptions = (
        await session.execute(
            select(WebhookSubscription).where(
                WebhookSubscription.project_id == project.id, WebhookSubscription.active.is_(True)
            )
        )
    ).scalars().all()

    delivered: list[Notification] = []
    for notification in due:
        event_type = _event_type_for_notification(notification)
        matching = [s for s in subscriptions if event_type in s.event_types]
        if not matching:
            continue
        payload = {
            "event": event_type,
            "notification_id": str(notification.id),
            "project_id": str(project.id),
            "severity": notification.severity,
            "downstream_consequence": notification.downstream_consequence,
            "collapsed_count": notification.collapsed_count,
        }
        all_ok = True
        for subscription in matching:
            all_ok = await deliver_webhook(subscription, payload) and all_ok
        if all_ok:
            notification.delivered_via = "webhook"
            notification.sent_at = now
            await session.flush()
            await record_foresight_audit_event(
                session, project_id=project.id, action="notification_delivered", actor_id=None,
                notification_id=notification.id, detail={"via": "webhook", "subscriptions": len(matching)},
            )
            delivered.append(notification)

    return delivered
