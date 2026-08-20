"""Layer A observability dashboard's backend proxy
(task-layer-A-observability-dashboard-prompt.txt). Layer A's own admin API
is HTTP Basic auth, completely separate from this backend's JWT-bearer auth
— its credentials are held server-side only (app/layer_a/config.py) and
never reach the browser. Every route here is administrator-only, the same
tier app/api/admin.py's org-wide surfaces already use — this is sensitive
infra-status data, not project-scoped.

Live status proxies Layer A synchronously per-request (cheap, admin-only,
freshness matters); trend/alert history always reads Postgres, since that
durable copy is the entire point of this feature (Layer A's own
HealthHistoryStore is a 200-entry, in-memory-only ring buffer).
"""

import secrets
import uuid
from datetime import datetime, timezone
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_org_id, require_org_administrator
from app.api.schemas import (
    LayerAAccountOut,
    LayerAAlertConfigOut,
    LayerAAlertConfigUpdate,
    LayerAAlertDeliveryOut,
    LayerAAlertOut,
    LayerAConflictEventOut,
    LayerAConflictSummary,
    LayerAHealthSnapshotOut,
    LayerAOpenAlertCountOut,
)
from app.core.db import get_session
from app.identity.models import User
from app.layer_a.client import LayerAAdminClient, LayerAConfigError
from app.layer_a.models import LayerAAlert, LayerAAlertConfig, LayerAAlertDelivery, LayerAConflictEvent, LayerAHealthSnapshot

router = APIRouter(prefix="/admin/layer-a", tags=["layer-a"])


def _client() -> LayerAAdminClient:
    try:
        return LayerAAdminClient()
    except LayerAConfigError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


async def _proxy_layer_a(coro):
    try:
        return await coro
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Layer A unreachable: {e}") from e


async def _org_account_ids(session: AsyncSession, org_id: uuid.UUID) -> set[str]:
    """Layer A account ids this organisation has actually seen.

    The Layer A gateway is a single shared process with no tenant concept —
    `client.list_accounts()` returns every account it runs, for everybody. The
    proxy endpoints below had no `org_id` at all, so any organisation
    administrator could read every other tenant's WhatsApp/WeChat account
    names, channel types and health. That is a cross-tenant data leak behind
    an authenticated role, not a cosmetic scoping miss.

    `LayerAHealthSnapshot` is the only org↔account mapping that exists: the
    poller writes one row per (organisation_id, account_id) it observes. So
    scoping is derived from what this org has polled, which **fails closed** —
    an account the org has never polled is invisible rather than leaked. That
    is the correct direction for this bug, and the residual cost (a
    just-provisioned account not appearing until the first poll) is a delay,
    not a loss.

    The real fix is that this whole surface is platform-operator, not
    tenant-facing — the gateway's PIDs and cross-tenant worker table are
    infrastructure state no tenant should reach at all. That needs a platform
    role this codebase does not yet have, so this closes the leak with the
    scoping that is available today.
    """
    return set(
        (
            await session.execute(
                select(LayerAHealthSnapshot.account_id)
                .where(LayerAHealthSnapshot.organisation_id == org_id)
                .distinct()
            )
        ).scalars().all()
    )


@router.get("/accounts", response_model=list[LayerAAccountOut])
async def list_layer_a_accounts(
    org_id: Annotated[uuid.UUID, Depends(get_org_id)],
    session: Annotated[AsyncSession, Depends(get_session)],
    _admin: Annotated[User, Depends(require_org_administrator)],
) -> list[dict]:
    client = _client()
    accounts = await _proxy_layer_a(client.list_accounts())
    visible = await _org_account_ids(session, org_id)
    return [a for a in accounts if a.get("accountId") in visible]


@router.get("/accounts/{account_id}", response_model=LayerAAccountOut)
async def get_layer_a_account(
    account_id: str,
    org_id: Annotated[uuid.UUID, Depends(get_org_id)],
    session: Annotated[AsyncSession, Depends(get_session)],
    _admin: Annotated[User, Depends(require_org_administrator)],
) -> dict:
    # Checked before the proxy call, so another tenant's account is
    # indistinguishable from one that does not exist — no existence oracle.
    if account_id not in await _org_account_ids(session, org_id):
        raise HTTPException(status_code=404, detail="unknown Layer A account")
    client = _client()
    account = await _proxy_layer_a(client.get_account(account_id))
    if account is None:
        raise HTTPException(status_code=404, detail="unknown Layer A account")
    return account


@router.get("/conflicts/live", response_model=list[LayerAConflictSummary])
async def list_layer_a_live_conflicts(
    _admin: Annotated[User, Depends(require_org_administrator)],
) -> list[dict]:
    """Layer A's own GET /admin/conflicts, proxied live — distinct from
    GET /conflicts below (the durable Postgres copy, unbounded, beyond
    Layer A's own 50-entry file cap)."""
    client = _client()
    return await _proxy_layer_a(client.list_conflicts())


@router.get("/accounts/{account_id}/trend", response_model=list[LayerAHealthSnapshotOut])
async def get_layer_a_account_trend(
    account_id: str,
    org_id: Annotated[uuid.UUID, Depends(get_org_id)],
    session: Annotated[AsyncSession, Depends(get_session)],
    _admin: Annotated[User, Depends(require_org_administrator)],
    limit: Annotated[int, Query(gt=0, le=5000)] = 1000,
) -> list[LayerAHealthSnapshot]:
    rows = (
        await session.execute(
            select(LayerAHealthSnapshot)
            .where(
                LayerAHealthSnapshot.organisation_id == org_id,
                LayerAHealthSnapshot.account_id == account_id,
            )
            .order_by(LayerAHealthSnapshot.recorded_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)


@router.get("/conflicts", response_model=list[LayerAConflictEventOut])
async def list_layer_a_conflict_events(
    org_id: Annotated[uuid.UUID, Depends(get_org_id)],
    session: Annotated[AsyncSession, Depends(get_session)],
    _admin: Annotated[User, Depends(require_org_administrator)],
) -> list[LayerAConflictEvent]:
    rows = (
        await session.execute(
            select(LayerAConflictEvent)
            .where(LayerAConflictEvent.organisation_id == org_id)
            .order_by(LayerAConflictEvent.detected_at.desc())
        )
    ).scalars().all()
    return list(rows)


@router.get("/alerts", response_model=list[LayerAAlertOut])
async def list_layer_a_alerts(
    org_id: Annotated[uuid.UUID, Depends(get_org_id)],
    session: Annotated[AsyncSession, Depends(get_session)],
    _admin: Annotated[User, Depends(require_org_administrator)],
    state: Annotated[str | None, Query()] = None,
    alert_type: Annotated[str | None, Query()] = None,
) -> list[LayerAAlert]:
    stmt = select(LayerAAlert).where(LayerAAlert.organisation_id == org_id)
    if state is not None:
        stmt = stmt.where(LayerAAlert.state == state)
    if alert_type is not None:
        stmt = stmt.where(LayerAAlert.alert_type == alert_type)
    rows = (await session.execute(stmt.order_by(LayerAAlert.opened_at.desc()))).scalars().all()
    return list(rows)


async def _get_alert(session: AsyncSession, org_id: uuid.UUID, alert_id: uuid.UUID) -> LayerAAlert:
    alert = (
        await session.execute(
            select(LayerAAlert).where(LayerAAlert.id == alert_id, LayerAAlert.organisation_id == org_id)
        )
    ).scalar_one_or_none()
    if alert is None:
        raise HTTPException(status_code=404, detail="alert not found")
    return alert


@router.post("/alerts/{alert_id}/acknowledge", response_model=LayerAAlertOut)
async def acknowledge_layer_a_alert(
    alert_id: uuid.UUID,
    org_id: Annotated[uuid.UUID, Depends(get_org_id)],
    session: Annotated[AsyncSession, Depends(get_session)],
    admin: Annotated[User, Depends(require_org_administrator)],
) -> LayerAAlert:
    """Acknowledging is resolution only for session_conflict — that alert
    type is event-shaped (a conflict already happened, it doesn't "clear"
    on its own), unlike sustained_disconnect/reconnect_flapping, which are
    condition-shaped and auto-resolve once the poller observes the
    condition has cleared (app/layer_a/alerts.py). Acknowledging the other
    two types is still recorded (who/when saw it) but doesn't force
    state='resolved' — the condition itself decides that."""
    alert = await _get_alert(session, org_id, alert_id)
    alert.acknowledged_by = admin.id
    alert.acknowledged_at = datetime.now(timezone.utc)
    if alert.alert_type == "session_conflict" and alert.state == "open":
        alert.state = "resolved"
        alert.resolved_at = alert.acknowledged_at
    await session.commit()
    return alert


@router.get("/alerts/{alert_id}/deliveries", response_model=list[LayerAAlertDeliveryOut])
async def list_layer_a_alert_deliveries(
    alert_id: uuid.UUID,
    org_id: Annotated[uuid.UUID, Depends(get_org_id)],
    session: Annotated[AsyncSession, Depends(get_session)],
    _admin: Annotated[User, Depends(require_org_administrator)],
) -> list[LayerAAlertDelivery]:
    await _get_alert(session, org_id, alert_id)  # 404 if the alert isn't this org's
    rows = (
        await session.execute(
            select(LayerAAlertDelivery)
            .where(LayerAAlertDelivery.alert_id == alert_id, LayerAAlertDelivery.organisation_id == org_id)
            .order_by(LayerAAlertDelivery.attempted_at.desc())
        )
    ).scalars().all()
    return list(rows)


def _config_out(config: LayerAAlertConfig) -> LayerAAlertConfigOut:
    return LayerAAlertConfigOut(
        id=config.id,
        organisation_id=config.organisation_id,
        enabled=config.enabled,
        sustained_disconnect_minutes=config.sustained_disconnect_minutes,
        reconnect_attempt_threshold=config.reconnect_attempt_threshold,
        reconnect_attempt_window_minutes=config.reconnect_attempt_window_minutes,
        webhook_url=config.webhook_url,
        webhook_enabled=config.webhook_enabled,
        webhook_configured=config.webhook_secret is not None,
        email_recipients=config.email_recipients,
        email_enabled=config.email_enabled,
        updated_by=config.updated_by,
        updated_at=config.updated_at,
    )


async def _get_or_create_config(session: AsyncSession, org_id: uuid.UUID) -> LayerAAlertConfig:
    config = (
        await session.execute(
            select(LayerAAlertConfig).where(LayerAAlertConfig.organisation_id == org_id)
        )
    ).scalar_one_or_none()
    if config is None:
        config = LayerAAlertConfig(organisation_id=org_id)
        session.add(config)
        await session.flush()
    return config


@router.get("/config", response_model=LayerAAlertConfigOut)
async def get_layer_a_alert_config(
    org_id: Annotated[uuid.UUID, Depends(get_org_id)],
    session: Annotated[AsyncSession, Depends(get_session)],
    _admin: Annotated[User, Depends(require_org_administrator)],
) -> LayerAAlertConfigOut:
    config = await _get_or_create_config(session, org_id)
    # Read the response's fields before commit, not after — session.commit()
    # expires every attribute on every object it touched, and a plain
    # (non-awaited) attribute access on an expired object outside an
    # explicit async ORM call raises MissingGreenlet trying to implicitly
    # refetch it. Building the Pydantic response first sidesteps that
    # entirely rather than requiring a session.refresh() after every write.
    result = _config_out(config)
    await session.commit()
    return result


@router.put("/config", response_model=LayerAAlertConfigOut)
async def update_layer_a_alert_config(
    body: LayerAAlertConfigUpdate,
    org_id: Annotated[uuid.UUID, Depends(get_org_id)],
    session: Annotated[AsyncSession, Depends(get_session)],
    admin: Annotated[User, Depends(require_org_administrator)],
) -> LayerAAlertConfigOut:
    config = await _get_or_create_config(session, org_id)
    updates = body.model_dump(exclude_unset=True)

    new_url = updates.get("webhook_url", config.webhook_url)
    if "webhook_url" in updates and new_url != config.webhook_url:
        # A changed (or newly set) URL gets a freshly generated secret —
        # mirrors WebhookSubscriptionCreated's "return the secret exactly
        # once" pattern; the caller sees whether one exists via
        # webhook_configured, never the secret's value itself, after this
        # response.
        config.webhook_secret = secrets.token_hex(32) if new_url else None

    for field, value in updates.items():
        setattr(config, field, value)
    config.updated_by = admin.id
    await session.flush()
    # updated_at's onupdate=func.now() value is only known once the UPDATE
    # this flush just issued actually runs server-side — SQLAlchemy expires
    # that column rather than guessing it, so an explicit async refresh is
    # needed before a plain attribute read; a bare post-commit read would
    # otherwise try an implicit synchronous reload and raise MissingGreenlet
    # (see get_layer_a_alert_config's comment for the general "read before
    # commit" half of this same class of bug).
    await session.refresh(config)
    result = _config_out(config)
    await session.commit()
    return result


@router.get("/alerts/open/count", response_model=LayerAOpenAlertCountOut)
async def count_open_layer_a_alerts(
    org_id: Annotated[uuid.UUID, Depends(get_org_id)],
    session: Annotated[AsyncSession, Depends(get_session)],
    _admin: Annotated[User, Depends(require_org_administrator)],
) -> dict:
    """The always-visible TopNav badge's data source (locked design
    decision: an active alert must be visible before an admin even opens
    the dashboard, not buried inside it) — a lighter read than the full
    /alerts list."""
    count = (
        await session.execute(
            select(func.count())
            .select_from(LayerAAlert)
            .where(LayerAAlert.organisation_id == org_id, LayerAAlert.state == "open")
        )
    ).scalar_one()
    return {"count": count}
