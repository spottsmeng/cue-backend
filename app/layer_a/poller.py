import logging
import uuid
from datetime import datetime, timezone as dt_timezone

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import get_settings
from app.core.db import async_session_factory, org_scoped_transaction
from app.layer_a.alerts import evaluate_alerts
from app.layer_a.client import LayerAAdminClient
from app.layer_a.config import get_layer_a_admin_settings
from app.layer_a.models import LayerAAlertConfig, LayerAConflictEvent, LayerAHealthSnapshot

logger = logging.getLogger("app.layer_a.poller")

_WORKER_STATUSES = {"connecting", "connected", "reconnecting", "unhealthy", "disconnected"}


def _status_from_worker_status(value: str | None) -> str:
    # Layer A's own admin API synthesizes "unknown" when no state exists yet
    # (accountSummary()); WorkerStatus proper is the other 5 values.
    return value if value in _WORKER_STATUSES else "unknown"


def _epoch_ms_to_datetime(value: int | float | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, tz=dt_timezone.utc)


def _epoch_seconds_to_datetime(value: int | float | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=dt_timezone.utc)


async def _discover_layer_a_orgs() -> list[uuid.UUID]:
    """organisation_ids opted into Layer A observability
    (layer_a_alert_config.enabled=true) — same RLS-bypass discovery shape
    app/foresight/worker.py's own _discover_active_projects establishes,
    for the same reason: "which orgs have this enabled" is a platform-level
    question no single tenant's RLS context could answer on its own."""
    engine = create_async_engine(get_settings().migration_database_url)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text("SELECT organisation_id FROM layer_a_alert_config WHERE enabled = true")
                )
            ).all()
            return [row.organisation_id for row in rows]
    finally:
        await engine.dispose()


async def _insert_poll_snapshots(
    session: AsyncSession, organisation_id: uuid.UUID, accounts: list[dict]
) -> list[str]:
    """One row per account, source='poll', from Layer A's live
    GET /admin/accounts read — the only source that carries `detail`
    (lastDisconnectReason, lastDisconnectStatusCode, healthy, riskTier;
    see health-history.ts's own HealthHistoryEntry, which never carries
    `detail` at all — GET .../health-history is WorkerState only).

    Returns the account_ids that just transitioned *into*
    lastDisconnectStatusCode == 440 this sweep (i.e. their previous poll
    snapshot, if any, did not already read 440) — the session_conflict
    alert's poll-sourced trigger. Comparing against the previous poll
    snapshot, not just "is 440 right now," matters: Layer A only overwrites
    lastDisconnectStatusCode on the *next* close event, so without this
    check a still-disconnected account would re-report the same stale 440
    forever and this function would return it as "newly conflicted" on
    every sweep. The alerts table's partial unique index would absorb a
    literal duplicate open attempt harmlessly, but this still avoids
    re-running that evaluation path pointlessly every single tick."""
    now = datetime.now(dt_timezone.utc)
    newly_conflicted: list[str] = []
    for account in accounts:
        account_id = account["accountId"]
        detail = account.get("detail") or {}
        status_code = detail.get("lastDisconnectStatusCode")

        previous_code = (
            await session.execute(
                select(LayerAHealthSnapshot.last_disconnect_status_code)
                .where(
                    LayerAHealthSnapshot.organisation_id == organisation_id,
                    LayerAHealthSnapshot.account_id == account_id,
                    LayerAHealthSnapshot.source == "poll",
                )
                .order_by(LayerAHealthSnapshot.recorded_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if status_code == 440 and previous_code != 440:
            newly_conflicted.append(account_id)

        stmt = (
            pg_insert(LayerAHealthSnapshot)
            .values(
                organisation_id=organisation_id,
                account_id=account_id,
                source="poll",
                recorded_at=now,
                status=_status_from_worker_status(account.get("status")),
                connect_attempts=detail.get("connectAttempts") or 0,
                last_error=account.get("lastError"),
                last_disconnect_reason=detail.get("lastDisconnectReason"),
                last_disconnect_status_code=status_code,
                healthy=account.get("healthy"),
                risk_tier=account.get("riskTier"),
            )
            .on_conflict_do_nothing(
                index_elements=["organisation_id", "account_id", "source", "recorded_at"],
                index_where=text("source = 'poll'"),
            )
        )
        await session.execute(stmt)
    return newly_conflicted


async def _insert_new_transitions(
    session: AsyncSession, organisation_id: uuid.UUID, account_id: str, history: list[dict]
) -> None:
    """Backfills Layer A's own in-memory 200-entry ring buffer
    (HealthHistoryStore) into durable storage. GET .../health-history has no
    since-cursor server-side, so the cursor lives here instead — per
    account, per org — the newest recorded_at already durably stored;
    only strictly-newer entries are inserted.

    No ON CONFLICT DO NOTHING here, deliberately — unlike poll rows,
    transition rows carry no DB-level uniqueness constraint on
    (account, source, recorded_at) at all (see the model's own
    __table_args__ comment): Layer A's millisecond-resolution timestamps
    can legitimately collide between two distinct, closely-spaced real
    transitions (e.g. "connecting" immediately followed by "connected"),
    and a same-timestamp collision must insert both, not silently drop the
    second. Cross-sweep dedup is entirely the cursor's job."""
    cursor = (
        await session.execute(
            select(func.max(LayerAHealthSnapshot.recorded_at)).where(
                LayerAHealthSnapshot.organisation_id == organisation_id,
                LayerAHealthSnapshot.account_id == account_id,
                LayerAHealthSnapshot.source == "transition",
            )
        )
    ).scalar_one_or_none()

    for entry in history:
        recorded_at = _epoch_ms_to_datetime(entry.get("recordedAt"))
        if recorded_at is None or (cursor is not None and recorded_at <= cursor):
            continue
        stmt = (
            pg_insert(LayerAHealthSnapshot)
            .values(
                organisation_id=organisation_id,
                account_id=account_id,
                source="transition",
                recorded_at=recorded_at,
                status=_status_from_worker_status(entry.get("status")),
                connect_attempts=entry.get("connectAttempts") or 0,
                last_connected_at=_epoch_ms_to_datetime(entry.get("lastConnectedAt")),
                # WorkerState.lastMessageTimestamp is unix *seconds* (newest
                # buffered message), unlike recordedAt/lastConnectedAt which
                # are epoch ms (Date.now()) — see health-history.ts.
                last_message_timestamp=_epoch_seconds_to_datetime(entry.get("lastMessageTimestamp")),
                last_error=entry.get("lastError"),
            )
        )
        await session.execute(stmt)


async def _insert_new_conflicts(
    session: AsyncSession, organisation_id: uuid.UUID, conflicts: list[dict]
) -> list[dict]:
    """Same since-cursor pattern as transitions above, against
    detected_at. Returns the raw event dicts that passed the cursor filter
    — every one of them is, by construction, newer than anything already
    durably stored for this org, so all of them are genuinely new
    session_conflict triggers (see app/layer_a/alerts.py)."""
    cursor = (
        await session.execute(
            select(func.max(LayerAConflictEvent.detected_at)).where(
                LayerAConflictEvent.organisation_id == organisation_id,
            )
        )
    ).scalar_one_or_none()

    new_events: list[dict] = []
    for event in conflicts:
        detected_at = _epoch_ms_to_datetime(event.get("detectedAt"))
        if detected_at is None or (cursor is not None and detected_at <= cursor):
            continue
        new_events.append(event)
        stmt = (
            pg_insert(LayerAConflictEvent)
            .values(
                organisation_id=organisation_id,
                detected_at=detected_at,
                refused_pid=event["refusedPid"],
                owner_pid=event["ownerPid"],
            )
            .on_conflict_do_nothing(
                index_elements=["organisation_id", "detected_at", "refused_pid"]
            )
        )
        await session.execute(stmt)
    return new_events


async def _poll_one_organisation(
    session: AsyncSession, organisation_id: uuid.UUID, client: LayerAAdminClient
) -> None:
    accounts = await client.list_accounts()
    async with org_scoped_transaction(session, organisation_id):
        config = (
            await session.execute(
                select(LayerAAlertConfig).where(LayerAAlertConfig.organisation_id == organisation_id)
            )
        ).scalar_one_or_none()
        if config is None or not config.enabled:
            return  # opted out since _discover_layer_a_orgs ran — nothing to do

        newly_conflicted_accounts = await _insert_poll_snapshots(session, organisation_id, accounts)
        for account in accounts:
            history = await client.get_health_history(account["accountId"])
            await _insert_new_transitions(session, organisation_id, account["accountId"], history)
        conflicts = await client.list_conflicts()
        new_conflict_events = await _insert_new_conflicts(session, organisation_id, conflicts)

        await evaluate_alerts(
            session, organisation_id, config,
            account_ids=[a["accountId"] for a in accounts],
            newly_conflicted_accounts=newly_conflicted_accounts,
            new_conflict_events=new_conflict_events,
        )


async def run_layer_a_poll_sweep(ctx: dict | None = None) -> int:
    """The arq cron job body — also directly callable (by tests, or a
    one-off ops invocation) without a running worker/broker, the same shape
    app/foresight/worker.py's own run_foresight_sweep establishes. Returns
    the number of organisations successfully polled."""
    settings = get_layer_a_admin_settings()
    if not (settings.base_url and settings.username and settings.password):
        logger.info("Layer A admin credentials not configured — skipping poll sweep")
        return 0

    targets = await _discover_layer_a_orgs()
    client = LayerAAdminClient(settings)
    swept = 0
    for organisation_id in targets:
        async with async_session_factory() as session:
            try:
                await _poll_one_organisation(session, organisation_id, client)
                swept += 1
            except Exception:
                await session.rollback()
                logger.exception("Layer A poll sweep failed for organisation %s", organisation_id)
    return swept
