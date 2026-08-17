import json
import logging
from datetime import datetime, timezone
from typing import AsyncIterator

import httpx

from app.capture.adapters.errors import CaptureConfigError
from app.capture.config import WhatsAppSettings, get_whatsapp_settings
from app.capture.schema import (
    ChannelHealthResult,
    RawCapturedMedia,
    RawCapturedMessage,
    compute_payload_hash,
)
from app.models import Channel

logger = logging.getLogger("cue.capture.whatsapp")


class WhatsAppAdapter:
    """PRD §9.1: the governed companion-session/MDM-handset route hosted in
    Pico's own tenant — the only route to reading consumer WhatsApp group
    chats (the WhatsApp Business Cloud API cannot). `session_endpoint`
    names Pico's own companion-session gateway, not a public WhatsApp API;
    its exact request/response shape was originally this adapter's own
    reasonable contract for that gateway (REST, bearer-token, `channel.
    external_ref` as the WhatsApp group id), not a documented external
    spec, at a time this whole class was genuinely credential-blocked
    (Prompt 11's own stated scope): code-complete, never live-tested.

    That status is stale as of `layer-A/` (this same repo) becoming the
    real companion-session gateway `session_endpoint` points at: a real
    WhatsApp account is linked, `test_layer_a_contract.py` drives this
    exact class against a real running Layer A process over real HTTP, and
    `list_conversations`/`add_to_allowlist`/`remove_from_allowlist` below
    ('Layer B Channel Picker' prompt) have been live-verified against that
    linked account, not just the fixture contract server. WeChat Work and
    Graph remain genuinely credential-blocked — this class no longer is.
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

    async def list_conversations(self) -> list[dict]:
        """`GET /conversations` (Machine API) — real, named WhatsApp
        conversations Layer A has discovered, whether or not anything's
        been captured from them yet. Unlike every method above, this isn't
        scoped to an existing `Channel` at all: it backs the *pre*-attach
        picker ('Layer B Channel Picker — Implementation Prompt.txt'), so
        there's no `channel.external_ref` to key off of yet — Layer A
        resolves the sole configured whatsapp account itself, 404ing if
        that's ever ambiguous (see app/api/channels.py's own handling of
        that case). Deliberately not cached beyond the caller's own
        request: Layer A resolves names live, not from a static list."""
        async with httpx.AsyncClient(timeout=30, headers=self._headers()) as client:
            response = await client.get(f"{self._settings.session_endpoint}/conversations")
            response.raise_for_status()
            return response.json().get("conversations", [])

    async def add_to_allowlist(self, channel_ref: str) -> list[str]:
        """`POST /conversations/allowlist/add` — grants Level C capture
        permission for exactly one conversation, without touching any other
        conversation an operator already designated through Layer A's own
        ops console (`allowlist-store.ts`'s `add()`, never `set()`).
        **Attaching a WhatsApp channel to a project must call this** — see
        app/api/channels.py's `attach_channel`. Idempotent: calling this on
        an already-designated conversation is a documented no-op."""
        async with httpx.AsyncClient(timeout=30, headers=self._headers()) as client:
            response = await client.post(
                f"{self._settings.session_endpoint}/conversations/allowlist/add",
                json={"channelRef": channel_ref},
            )
            response.raise_for_status()
            return response.json().get("channelRefs", [])

    async def remove_from_allowlist(self, channel_ref: str) -> list[str]:
        """`POST /conversations/allowlist/remove` — the matching revoke.
        **Detaching a WhatsApp channel from a project must call this** —
        see app/api/channels.py's `detach_channel`. Without it, a detached
        channel keeps silently capturing on Layer A's side with nowhere in
        Layer B left to receive it."""
        async with httpx.AsyncClient(timeout=30, headers=self._headers()) as client:
            response = await client.post(
                f"{self._settings.session_endpoint}/conversations/allowlist/remove",
                json={"channelRef": channel_ref},
            )
            response.raise_for_status()
            return response.json().get("channelRefs", [])

    async def fetch_media(self, channel: Channel, uri: str) -> bytes:
        """Same "this adapter's own reasonable contract for Pico's
        companion-session gateway, not a documented external spec" posture
        as every other method on this class (see class docstring) —
        `/media/{media_id}` returning the raw bytes directly is the natural
        counterpart to `/messages` and `/send` above."""
        async with httpx.AsyncClient(timeout=60, headers=self._headers()) as client:
            response = await client.get(f"{self._settings.session_endpoint}/media/{uri}")
            response.raise_for_status()
            return response.content


def _to_raw_media(entries: list[dict] | None) -> list[RawCapturedMedia]:
    """`NormalisedMediaRef[] | null` (layer-A/src/types/connector.ts) ->
    `RawCapturedMedia` — Layer A's baileys connector already classifies
    every WhatsApp attachment kind Layer B's contract has a slot for
    (image/document/voice_note, layer-A/src/connectors/whatsapp-baileys/
    normalise.ts) and puts it on the wire; this was the one adapter that
    never read it back off, silently dropping every attachment while still
    capturing its caption as `text`. `uri` is the same account-scoped
    reference `fetch_media` below already knows how to resolve — no new
    fetch path needed, just wiring this one field through."""
    return [
        RawCapturedMedia(kind=entry["kind"], uri=entry["uri"], filename=entry.get("filename"))
        for entry in entries or []
    ]


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
        media=_to_raw_media(raw.get("media")),
    )
