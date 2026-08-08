import logging
from datetime import datetime
from typing import AsyncIterator

import httpx

from app.capture.adapters.errors import CaptureConfigError
from app.capture.config import WeChatWorkSettings, get_wechat_work_settings
from app.capture.schema import ChannelHealthResult, RawCapturedMessage
from app.models import Channel

logger = logging.getLogger("cue.capture.wechat_work")

_QYAPI_BASE = "https://qyapi.weixin.qq.com/cgi-bin"


class WeChatWorkAdapter:
    """PRD §9.2: WeChat Work (企业微信) external-contact channel + the
    official Session Archiving API (会话存档) — the compliant, audited
    route, vs. reading a vendor's personal WeChat directly (not possible).

    Real Session Archiving message *content* is end-to-end encrypted with a
    private key WeChat Work provisions per corp — decrypting archived
    records requires the vendor's dedicated SDK (a C/Go binary WeChat Work
    publishes, not a documented plain-HTTP contract), so a from-scratch
    httpx client can authenticate (corp_id/corp_secret -> access_token,
    below) but cannot itself decrypt message bodies without also shelling
    out to or binding that SDK — named here explicitly rather than silently
    faked. `_decrypt_archive_chunk` is the seam that SDK integration fills
    in once `session_archive_secret` (the per-corp decryption key) is
    genuinely available; this adapter is credential-blocked in this
    environment either way (Prompt 11's own stated scope).
    """

    def __init__(self, settings: WeChatWorkSettings | None = None):
        settings = settings or get_wechat_work_settings()
        if not (settings.corp_id and settings.corp_secret and settings.session_archive_secret):
            raise CaptureConfigError(
                "channel_types code 'wechat' requires CUE_WECHAT_CORP_ID, _CORP_SECRET "
                "and _SESSION_ARCHIVE_SECRET"
            )
        self._settings = settings
        self._access_token: str | None = None

    async def _get_access_token(self, client: httpx.AsyncClient) -> str:
        if self._access_token is not None:
            return self._access_token
        response = await client.get(
            f"{_QYAPI_BASE}/gettoken",
            params={"corpid": self._settings.corp_id, "corpsecret": self._settings.corp_secret},
        )
        response.raise_for_status()
        body = response.json()
        if body.get("errcode"):
            raise RuntimeError(f"failed to acquire WeChat Work access_token: {body}")
        self._access_token = body["access_token"]
        return self._access_token

    async def fetch_backlog(
        self, channel: Channel, since: datetime | None = None
    ) -> AsyncIterator[RawCapturedMessage]:
        async with httpx.AsyncClient(timeout=30) as client:
            token = await self._get_access_token(client)
            response = await client.post(
                f"{_QYAPI_BASE}/msgaudit/get_group_chat",
                params={"access_token": token},
                json={"roomid": channel.external_ref, "seq": 0, "limit": 1000},
            )
            response.raise_for_status()
            for raw in self._decrypt_archive_chunk(response.json()):
                sent_at = raw["sent_at"]
                if since is not None and sent_at <= since:
                    continue
                yield raw["message"]

    async def stream(self, channel: Channel) -> AsyncIterator[RawCapturedMessage]:
        # Session Archiving has no live-push subscription of its own — the
        # official integration pattern is polling `get_group_chat` with an
        # increasing `seq`, which fetch_backlog already does; a real
        # implementation would loop that on an interval, deferred to the
        # ingestion queue's own scheduling (item 11) rather than an
        # adapter-internal sleep loop.
        async for message in self.fetch_backlog(channel):
            yield message

    async def send(self, channel: Channel, to_external_id: str, text: str) -> None:
        async with httpx.AsyncClient(timeout=30) as client:
            token = await self._get_access_token(client)
            response = await client.post(
                f"{_QYAPI_BASE}/message/send",
                params={"access_token": token},
                json={"touser": to_external_id, "msgtype": "text", "text": {"content": text}},
            )
            response.raise_for_status()

    async def health(self, channel: Channel) -> ChannelHealthResult:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await self._get_access_token(client)
            return ChannelHealthResult(healthy=True)
        except httpx.HTTPError as e:
            logger.warning("wechat work health check failed for channel=%s: %s", channel.id, e)
            return ChannelHealthResult(healthy=False, detail={"error": str(e)})

    def _decrypt_archive_chunk(self, raw_response: dict) -> list[dict]:
        """See class docstring — real decryption needs WeChat Work's own
        SDK bound against `session_archive_secret`, not implemented here.
        Raises rather than silently returning nothing, so a genuinely
        credentialed run fails loudly instead of reporting an empty channel
        as healthy."""
        raise NotImplementedError(
            "WeChat Work Session Archiving message decryption requires binding the "
            "vendor's own SDK against CUE_WECHAT_SESSION_ARCHIVE_SECRET — not implemented"
        )
