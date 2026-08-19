import asyncio
import hashlib
import hmac
import json
import logging
import smtplib
import uuid
from email.message import EmailMessage

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.layer_a.config import LayerAEmailSettings, get_layer_a_email_settings
from app.layer_a.models import LayerAAlert, LayerAAlertConfig, LayerAAlertDelivery

logger = logging.getLogger("app.layer_a.notification")


def _alert_payload(alert: LayerAAlert) -> dict:
    return {
        "alert_type": alert.alert_type,
        "account_id": alert.account_id,
        "severity": alert.severity,
        "opened_at": alert.opened_at.isoformat(),
        "condition_detail": alert.condition_detail,
    }


async def deliver_layer_a_webhook(config: LayerAAlertConfig, alert: LayerAAlert) -> bool:
    """Copies app/foresight/notification.py's deliver_webhook shape exactly
    (HMAC-SHA256 signed body, never raises). Not reusing WebhookSubscription/
    deliver_webhook directly — that table is per-project, write-role-gated,
    event-typed for commitment/risk/deviation; this is a different,
    org-admin-scoped concern (LayerAAlertConfig.webhook_url), so a parallel
    function is more honest than overloading one table for two unrelated
    subscription models."""
    if not (config.webhook_enabled and config.webhook_url and config.webhook_secret):
        return False
    body = json.dumps(_alert_payload(alert), sort_keys=True).encode()
    signature = hmac.new(config.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                config.webhook_url,
                content=body,
                headers={"content-type": "application/json", "x-cue-signature": signature},
            )
            response.raise_for_status()
        return True
    except httpx.HTTPError as e:
        logger.warning("layer_a webhook delivery failed for alert=%s: %s", alert.id, e)
        return False


async def deliver_layer_a_email(config: LayerAAlertConfig, alert: LayerAAlert) -> bool:
    """Genuinely new infrastructure — confirmed nothing general-purpose
    existed before this (app/capture/adapters/imap_smtp.py's send() is a
    capture-reply adapter, not an alerting primitive). Same stdlib-smtplib-
    via-asyncio.to_thread shape as that adapter. Never raises, returns False
    on any socket/smtplib error — same "expected outcome, logged, visible
    on the delivery log" posture as the webhook path above."""
    settings = get_layer_a_email_settings()
    if not (config.email_enabled and config.email_recipients and settings.host):
        return False
    try:
        await asyncio.to_thread(_send_email_with_recipients, settings, alert, config.email_recipients)
        return True
    except (smtplib.SMTPException, OSError) as e:
        logger.warning("layer_a email delivery failed for alert=%s: %s", alert.id, e)
        return False


def _send_email_with_recipients(settings: LayerAEmailSettings, alert: LayerAAlert, recipients: list[str]) -> None:
    msg = EmailMessage()
    msg["From"] = settings.from_address or settings.username
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = f"CUE Layer A alert: {alert.alert_type}"
    msg.set_content(json.dumps(_alert_payload(alert), indent=2, sort_keys=True))
    with smtplib.SMTP(settings.host, settings.port) as conn:
        if settings.use_tls:
            conn.starttls()
        if settings.username and settings.password:
            conn.login(settings.username, settings.password)
        conn.send_message(msg)


async def deliver_layer_a_alert(
    session: AsyncSession, organisation_id: uuid.UUID, config: LayerAAlertConfig, alert: LayerAAlert
) -> None:
    """Fans a newly-opened alert out to every enabled destination, logging
    one LayerAAlertDelivery row per channel regardless of outcome — the
    "reviewable, not fired-and-forgotten" requirement extended to the
    destinations themselves. Not retried by the poller itself: a failed
    webhook/email is visible on the dashboard's delivery log; genuine
    retry-with-backoff is a documented v2 gap (see class docstring in
    app/layer_a/models.py's LayerAAlertDelivery), not an oversight."""
    session.add(
        LayerAAlertDelivery(
            alert_id=alert.id, organisation_id=organisation_id, channel="banner", success=True,
        )
    )
    if config.webhook_enabled:
        ok = await deliver_layer_a_webhook(config, alert)
        session.add(
            LayerAAlertDelivery(
                alert_id=alert.id, organisation_id=organisation_id, channel="webhook", success=ok,
                detail=None if ok else "delivery failed — see application logs",
            )
        )
    if config.email_enabled:
        ok = await deliver_layer_a_email(config, alert)
        session.add(
            LayerAAlertDelivery(
                alert_id=alert.id, organisation_id=organisation_id, channel="email", success=ok,
                detail=None if ok else "delivery failed — see application logs",
            )
        )
