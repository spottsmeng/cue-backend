import uuid
from datetime import datetime, timedelta, timezone as dt_timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.foresight.audit import record_foresight_audit_event
from app.foresight.models import Risk
from app.foresight.notification import dispatch_event
from app.foresight.threshold import resolve_threshold
from app.identity.models import Delegation, Membership
from app.models import Project

# FR-FOR-08 (PRD §6.9): "Support severity-based escalation chains with
# delegation-aware routing." Reuses app/identity/service.py's
# effective_roles/Delegation data model rather than inventing a new
# permission concept (Prompt 7's own explicit instruction) — but that
# module has no *role seniority* concept at all (it only answers "does user
# X hold role Y", never "which role is senior to which"), so ESCALATION_CHAIN
# below is a new, small, Foresight-local ordering, not a general-purpose
# addition to identity/service.py. project_manager -> producer ->
# administrator: the same seniority app/identity/service.py's own
# ADMIN_ROLES = {"administrator", "producer"} constant already implies
# (producer sits alongside administrator as an escalation-worthy tier,
# project_manager does not) — see that module for the precedent this reuses
# rather than reinvents.
#
# CUE-Tech-Stack.md §2.4 names Temporal (not arq) for "escalation chains...
# long-running, resumable, human-in-the-loop flows." This session
# deliberately does not add Temporal — a second, heavier piece of
# infrastructure — for what a periodic reconciliation sweep already covers
# correctly at this scale: an escalation is "is this Risk still
# unacknowledged past its threshold", checked on every scheduled run, the
# same "no new infrastructure dependency needed at this scale" call
# app/twin/graph.py's own module docstring already makes for a comparable
# tradeoff. Revisit if/when a genuinely long-running, multi-day approval
# workflow needs durable state a stateless sweep can't express.
ESCALATION_CHAIN: tuple[str, ...] = ("project_manager", "producer", "administrator")


async def _role_holders(session: AsyncSession, project_id: uuid.UUID, role: str) -> set[uuid.UUID]:
    """Every user who currently holds `role` on this project — via standing
    Membership or an active Delegation — mirrors
    app/identity/service.py's effective_roles' own two queries, inverted
    (that function answers "this user's roles"; this answers "this role's
    users")."""
    member_ids = (
        await session.execute(
            select(Membership.user_id).where(Membership.project_id == project_id, Membership.role == role)
        )
    ).scalars().all()
    delegate_ids = (
        await session.execute(
            select(Delegation.delegate_id).where(
                Delegation.project_id == project_id,
                Delegation.role == role,
                Delegation.revoked_at.is_(None),
                Delegation.expires_at > func.now(),
            )
        )
    ).scalars().all()
    return set(member_ids) | set(delegate_ids)


async def escalate_unacknowledged_risks(session: AsyncSession, project: Project) -> list[Risk]:
    """FR-FOR-08. An open, unacknowledged Risk that has sat for at least
    `escalation_hours` (FR-FOR-07's configured threshold) since it was
    either created or last escalated moves to the next role up the chain,
    and every current holder of that role (membership or active delegation)
    is notified (FR-NTF-05). A Risk already escalated to the top of the
    chain (administrator) is left alone — there is nowhere further to
    escalate to. Does not commit — caller (the arq-scheduled sweep) owns
    the transaction boundary."""
    threshold_hours = await resolve_threshold(session, project=project, metric="escalation_hours")
    now = datetime.now(dt_timezone.utc)
    cutoff = now - timedelta(hours=threshold_hours)

    stmt = select(Risk).where(
        Risk.project_id == project.id,
        Risk.status == "open",
        Risk.acknowledged_at.is_(None),
    )
    candidates = (await session.execute(stmt)).scalars().all()

    touched: list[Risk] = []
    for risk in candidates:
        last_action_at = risk.escalated_at or risk.created_at
        if last_action_at > cutoff:
            continue  # not yet due for (another) escalation

        current_index = ESCALATION_CHAIN.index(risk.escalated_to_role) if risk.escalated_to_role else -1
        next_index = current_index + 1
        if next_index >= len(ESCALATION_CHAIN):
            continue  # already at the top of the chain

        next_role = ESCALATION_CHAIN[next_index]
        risk.escalated_to_role = next_role
        risk.escalated_at = now
        await session.flush()

        await record_foresight_audit_event(
            session, project_id=project.id, action="risk_escalated", actor_id=None,
            risk_id=risk.id, detail={"escalated_to_role": next_role, "escalation_hours": threshold_hours},
        )

        recipients = await _role_holders(session, project.id, next_role)
        if recipients:
            await dispatch_event(
                session, project=project, event_type="risk", risk=risk, recipient_ids=recipients,
            )
        touched.append(risk)

    return touched
