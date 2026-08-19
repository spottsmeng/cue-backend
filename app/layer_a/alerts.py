import uuid
from datetime import datetime, timedelta, timezone as dt_timezone

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.layer_a.models import LayerAAlert, LayerAAlertConfig, LayerAHealthSnapshot
from app.layer_a.notification import deliver_layer_a_alert

# How far back sustained_disconnect's "walk backward to find when this
# account went disconnected" scan looks — bounded so a long-dead account
# doesn't force an unbounded table scan; 500 rows comfortably covers a
# multi-day gap at the poller's 1-minute cadence even before any
# transition-sourced backfill exists yet.
_DISCONNECT_LOOKBACK_ROWS = 500

_ALERT_SEVERITY = {
    "sustained_disconnect": "serious",
    "reconnect_flapping": "serious",
    "session_conflict": "critical",
}


async def evaluate_alerts(
    session: AsyncSession,
    organisation_id: uuid.UUID,
    config: LayerAAlertConfig,
    *,
    account_ids: list[str],
    newly_conflicted_accounts: list[str],
    new_conflict_events: list[dict],
) -> None:
    """Runs all three alert conditions for one organisation, called once per
    sweep tick right after that org's snapshots/conflicts are persisted
    (app/layer_a/poller.py) — alert conditions are pure functions of what
    was just persisted, so evaluating inline avoids any race between
    "snapshot written" and "alert evaluated against stale data."

    `account_ids` are every account this sweep observed live (drives
    sustained_disconnect / reconnect_flapping, per-account, condition-shaped
    — they auto-resolve once the condition clears). `newly_conflicted_
    accounts` and `new_conflict_events` are session_conflict's two
    independent triggers (event-shaped — they don't auto-resolve, see
    LayerAAlert's own docstring for why acknowledging is what resolves
    them)."""
    for account_id in account_ids:
        await _evaluate_sustained_disconnect(session, organisation_id, config, account_id)
        await _evaluate_reconnect_flapping(session, organisation_id, config, account_id)

    for account_id in newly_conflicted_accounts:
        await _open_alert(
            session, organisation_id, config, "session_conflict", account_id,
            {"status_code": 440},
        )
    for event in new_conflict_events:
        await _open_alert(
            session, organisation_id, config, "session_conflict", None,
            {"refused_pid": event["refusedPid"], "owner_pid": event["ownerPid"]},
        )


async def _evaluate_sustained_disconnect(
    session: AsyncSession, organisation_id: uuid.UUID, config: LayerAAlertConfig, account_id: str
) -> None:
    latest = (
        await session.execute(
            select(LayerAHealthSnapshot.status, LayerAHealthSnapshot.recorded_at)
            .where(
                LayerAHealthSnapshot.organisation_id == organisation_id,
                LayerAHealthSnapshot.account_id == account_id,
            )
            .order_by(LayerAHealthSnapshot.recorded_at.desc())
            .limit(1)
        )
    ).first()
    if latest is None:
        return
    if latest.status == "connected":
        await _resolve_alert(session, organisation_id, "sustained_disconnect", account_id)
        return

    recent = (
        await session.execute(
            select(LayerAHealthSnapshot.recorded_at, LayerAHealthSnapshot.status)
            .where(
                LayerAHealthSnapshot.organisation_id == organisation_id,
                LayerAHealthSnapshot.account_id == account_id,
            )
            .order_by(LayerAHealthSnapshot.recorded_at.desc())
            .limit(_DISCONNECT_LOOKBACK_ROWS)
        )
    ).all()
    # Walk backward from the most recent row to the first one that's still
    # part of this same disconnected run — its recorded_at is when the
    # account went disconnected. If the whole lookback window is uniformly
    # disconnected, this approximates to the oldest row in that window
    # rather than the true start (documented, not a correctness bug: it
    # only under-counts duration, never over-counts, so it can only delay
    # an alert, never falsely open one early).
    disconnected_since = recent[-1].recorded_at
    for row in recent:
        if row.status == "connected":
            break
        disconnected_since = row.recorded_at

    duration = datetime.now(dt_timezone.utc) - disconnected_since
    if duration > timedelta(minutes=config.sustained_disconnect_minutes):
        await _open_alert(
            session, organisation_id, config, "sustained_disconnect", account_id,
            {
                "disconnected_since": disconnected_since.isoformat(),
                "duration_minutes": round(duration.total_seconds() / 60, 1),
            },
        )


async def _evaluate_reconnect_flapping(
    session: AsyncSession, organisation_id: uuid.UUID, config: LayerAAlertConfig, account_id: str
) -> None:
    # connectAttempts resets to 0 on every successful connect
    # (layer-A/src/session-manager/index.ts's runWorker) — counting
    # transition-sourced rows recorded as connecting/reconnecting within
    # the window is robust to that reset, unlike a naive MAX-MIN over
    # connectAttempts would be. Each such row already represents one
    # distinct recorded transition (HealthHistoryStore.record fires once
    # per onStateChange call, not on a fixed poll tick), so no additional
    # "differs from the previous row" filter is needed to avoid
    # double-counting.
    window_start = datetime.now(dt_timezone.utc) - timedelta(minutes=config.reconnect_attempt_window_minutes)
    count = (
        await session.execute(
            select(func.count())
            .select_from(LayerAHealthSnapshot)
            .where(
                LayerAHealthSnapshot.organisation_id == organisation_id,
                LayerAHealthSnapshot.account_id == account_id,
                LayerAHealthSnapshot.source == "transition",
                LayerAHealthSnapshot.status.in_(["connecting", "reconnecting"]),
                LayerAHealthSnapshot.recorded_at >= window_start,
            )
        )
    ).scalar_one()

    if count >= config.reconnect_attempt_threshold:
        await _open_alert(
            session, organisation_id, config, "reconnect_flapping", account_id,
            {"connect_attempts_in_window": count, "window_minutes": config.reconnect_attempt_window_minutes},
        )
    else:
        await _resolve_alert(session, organisation_id, "reconnect_flapping", account_id)


async def _open_alert(
    session: AsyncSession,
    organisation_id: uuid.UUID,
    config: LayerAAlertConfig,
    alert_type: str,
    account_id: str | None,
    condition_detail: dict,
) -> None:
    """INSERT ... ON CONFLICT DO NOTHING against the partial unique index on
    (organisation_id, alert_type, account_id) WHERE state='open' — safe to
    call every sweep for a still-ongoing condition; only a row that's
    genuinely new (no existing open alert of this type/account) is actually
    inserted, and only that case triggers delivery."""
    stmt = (
        pg_insert(LayerAAlert)
        .values(
            organisation_id=organisation_id,
            alert_type=alert_type,
            account_id=account_id,
            severity=_ALERT_SEVERITY[alert_type],
            state="open",
            condition_detail=condition_detail,
        )
        .on_conflict_do_nothing(
            index_elements=["organisation_id", "alert_type", "account_id"],
            index_where=text("state = 'open'"),
        )
        .returning(LayerAAlert)
    )
    result = await session.execute(stmt)
    alert = result.scalar_one_or_none()
    if alert is not None:
        await deliver_layer_a_alert(session, organisation_id, config, alert)


async def _resolve_alert(
    session: AsyncSession, organisation_id: uuid.UUID, alert_type: str, account_id: str
) -> None:
    """Only for the two condition-shaped alert types (sustained_disconnect,
    reconnect_flapping) — session_conflict is event-shaped and only ever
    resolved by an explicit acknowledgement (app/api/layer_a_admin.py's
    acknowledge endpoint), never called from here."""
    await session.execute(
        update(LayerAAlert)
        .where(
            LayerAAlert.organisation_id == organisation_id,
            LayerAAlert.alert_type == alert_type,
            LayerAAlert.account_id == account_id,
            LayerAAlert.state == "open",
        )
        .values(state="resolved", resolved_at=func.now())
    )
