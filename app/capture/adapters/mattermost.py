import asyncio
import logging
from datetime import datetime, timezone
from typing import AsyncIterator

import httpx

from app.capture.adapters.errors import CaptureConfigError
from app.capture.config import MattermostSettings, get_mattermost_settings
from app.capture.schema import (
    ChannelHealthResult,
    RawCapturedMedia,
    RawCapturedMessage,
    compute_payload_hash,
)
from app.models import Channel

logger = logging.getLogger("cue.capture.mattermost")

# Between polls while `stream()` is being consumed — Mattermost's real-time
# events are a websocket API this adapter doesn't open a persistent
# connection for (no websocket client in this project's dependencies yet);
# polling the same REST endpoint fetch_backlog uses is a deliberate
# simplification, named here rather than silently passed off as push-based.
_POLL_INTERVAL_SECONDS = 5


class MattermostAdapter:
    """The team_collaboration capability's FOSS substitute for Microsoft
    Teams (CUE-PRD.md §9.3a) — a real self-hosted/managed Mattermost
    instance the bot account (CUE_MATTERMOST_BOT_TOKEN) has been added to
    `channel.external_ref`'s channel, not a sandbox. Genuinely
    live-testable, unlike WhatsApp/WeChat/Graph in this environment.
    """

    def __init__(self, settings: MattermostSettings | None = None):
        settings = settings or get_mattermost_settings()
        if not (settings.base_url and settings.bot_token):
            raise CaptureConfigError(
                "channel_types code 'mattermost' requires CUE_MATTERMOST_BASE_URL and _BOT_TOKEN"
            )
        self._settings = settings

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=f"{self._settings.base_url.rstrip('/')}/api/v4",
            headers={"Authorization": f"Bearer {self._settings.bot_token}"},
            timeout=30,
        )

    async def fetch_backlog(
        self, channel: Channel, since: datetime | None = None
    ) -> AsyncIterator[RawCapturedMessage]:
        since_ms = int(since.timestamp() * 1000) if since is not None else None
        page = 0
        async with self._client() as client:
            while True:
                response = await client.get(
                    f"/channels/{channel.external_ref}/posts", params={"page": page, "per_page": 200}
                )
                response.raise_for_status()
                body = response.json()
                order = body.get("order", [])
                if not order:
                    return
                posts = body["posts"]
                for post_id in order:
                    post = posts[post_id]
                    if since_ms is not None and post["create_at"] <= since_ms:
                        continue
                    yield _to_raw_message(post)
                if len(order) < 200:
                    return
                page += 1

    async def stream(self, channel: Channel) -> AsyncIterator[RawCapturedMessage]:
        last_seen = datetime.now(timezone.utc)
        while True:
            async for message in self.fetch_backlog(channel, since=last_seen):
                last_seen = max(last_seen, message.sent_at)
                yield message
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)

    async def send(self, channel: Channel, to_external_id: str, text: str) -> None:
        async with self._client() as client:
            response = await client.post(
                "/posts", json={"channel_id": channel.external_ref, "message": text}
            )
            response.raise_for_status()

    async def health(self, channel: Channel) -> ChannelHealthResult:
        try:
            async with self._client() as client:
                response = await client.get(f"/channels/{channel.external_ref}")
                response.raise_for_status()
            return ChannelHealthResult(healthy=True)
        except httpx.HTTPError as e:
            logger.warning("mattermost health check failed for channel=%s: %s", channel.id, e)
            return ChannelHealthResult(healthy=False, detail={"error": str(e)})

    async def fetch_media(self, channel: Channel, uri: str) -> bytes:
        """`uri` is a Mattermost file id (`_to_raw_message`'s own
        `post["file_ids"]`) — `GET /files/{file_id}` returns the raw file
        content directly (Mattermost's API, not a metadata wrapper)."""
        async with self._client() as client:
            response = await client.get(f"/files/{uri}")
            response.raise_for_status()
            return response.content


def _to_raw_message(post: dict) -> RawCapturedMessage:
    raw_bytes = (post.get("message") or "").encode("utf-8")
    # FR-CAP-12: Mattermost reports attached file ids on the post itself
    # (`file_ids`), not full metadata (a MIME type would need a second
    # `/files/{id}/info` call per file) — `kind` is left as the generic
    # "document"; item 6's media pipeline (app/capture/media.py) branches
    # on the actually-fetched bytes' content type where it needs to, not on
    # this adapter's own guess.
    media = [RawCapturedMedia(kind="document", uri=fid) for fid in post.get("file_ids") or []]
    return RawCapturedMessage(
        external_id=post["id"],
        sender_external_id=post["user_id"],
        sent_at=datetime.fromtimestamp(post["create_at"] / 1000, tz=timezone.utc),
        text=post.get("message"),
        raw_payload_hash=compute_payload_hash("mattermost", post["id"], raw_bytes),
        media=media,
    )
