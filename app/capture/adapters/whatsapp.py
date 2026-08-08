import json
import logging
from datetime import datetime, timezone
from typing import AsyncIterator

import httpx

from app.capture.adapters.errors import CaptureConfigError
from app.capture.config import WhatsAppSettings, get_whatsapp_settings
from app.capture.schema import ChannelHealthResult, RawCapturedMessage, compute_payload_hash
from app.models import Channel

logger = logging.getLogger("cue.capture.whatsapp")


class WhatsAppAdapter:
    """PRD §9.1: the governed companion-session/MDM-handset route hosted in
    Pico's own tenant — the only route to reading consumer WhatsApp group
    chats (the WhatsApp Business Cloud API cannot). `session_endpoint`
    names Pico's own companion-session gateway, not a public WhatsApp API;
    its exact request/response shape is Pico infrastructure this codebase
    has no access to describe precisely, so the shape below (REST,
    bearer-token, `channel.external_ref` as the WhatsApp group id) is this
    adapter's own reasonable contract for that gateway, not a documented
    external spec — it's genuinely credential-blocked in this environment
    (Prompt 11's own stated scope): code-complete, never live-tested.
    """

    def __init__(self, settings: WhatsAppSettings | None = None):
        settings = settings or get_whatsapp_settings()
        if not (settings.session_endpoint and settings.api_token):
            raise CaptureConfigError(
                "channel_types code 'whatsapp' requires CUE_WHATSAPP_SESSION_ENDPOINT "
                "and CUE_WHATSAPP_API_TOKEN"
            )
        self._settings = settings

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._settings.api_token}"}

    async def fetch_backlog(
        self, channel: Channel, since: datetime | None = None
    ) -> AsyncIterator[RawCapturedMessage]:
        params = {"group_id": channel.external_ref}
        if since is not None:
            params["since"] = since.isoformat()
        async with httpx.AsyncClient(timeout=30, headers=self._headers()) as client:
            response = await client.get(f"{self._settings.session_endpoint}/messages", params=params)
            response.raise_for_status()
            for raw in response.json().get("messages", []):
                yield _to_raw_message(raw)

    async def stream(self, channel: Channel) -> AsyncIterator[RawCapturedMessage]:
        params = {"group_id": channel.external_ref}
        async with httpx.AsyncClient(timeout=None, headers=self._headers()) as client:
            async with client.stream("GET", f"{self._settings.session_endpoint}/stream", params=params) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    yield _to_raw_message(json.loads(line))

    async def send(self, channel: Channel, to_external_id: str, text: str) -> None:
        async with httpx.AsyncClient(timeout=30, headers=self._headers()) as client:
            response = await client.post(
                f"{self._settings.session_endpoint}/send",
                json={"group_id": channel.external_ref, "to": to_external_id, "text": text},
            )
            response.raise_for_status()

    async def health(self, channel: Channel) -> ChannelHealthResult:
        try:
            async with httpx.AsyncClient(timeout=10, headers=self._headers()) as client:
                response = await client.get(
                    f"{self._settings.session_endpoint}/health", params={"group_id": channel.external_ref}
                )
                response.raise_for_status()
                return ChannelHealthResult(healthy=True, detail=response.json())
        except httpx.HTTPError as e:
            logger.warning("whatsapp health check failed for channel=%s: %s", channel.id, e)
            return ChannelHealthResult(healthy=False, detail={"error": str(e)})


def _to_raw_message(raw: dict) -> RawCapturedMessage:
    raw_bytes = raw.get("raw_bytes")
    if isinstance(raw_bytes, str):
        raw_bytes = raw_bytes.encode("utf-8")
    elif raw_bytes is None:
        raw_bytes = (raw.get("text") or "").encode("utf-8")
    return RawCapturedMessage(
        external_id=raw["id"],
        sender_external_id=raw["from"],
        sent_at=datetime.fromtimestamp(raw["timestamp"], tz=timezone.utc),
        text=raw.get("text"),
        raw_payload_hash=compute_payload_hash("whatsapp", raw["id"], raw_bytes),
    )
