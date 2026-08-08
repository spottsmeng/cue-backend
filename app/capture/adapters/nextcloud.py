import asyncio
import logging
from datetime import datetime, timezone
from typing import AsyncIterator

import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.capture.adapters.errors import CaptureConfigError
from app.capture.schema import ChannelHealthResult, RawCapturedMedia, RawCapturedMessage, compute_payload_hash
from app.documents.nextcloud_client import NextcloudWebDavClient
from app.models import Channel

logger = logging.getLogger("cue.capture.nextcloud")

_POLL_INTERVAL_SECONDS = 30


class NextcloudCaptureSettings(BaseSettings):
    """Deliberately separate from app/documents/config.py's SharePointSettings
    nextcloud_* fields (the write-back direction): capture reads
    `channel.external_ref` as the folder to watch per Channel row (a
    project can watch more than one folder), so the shared bit is only the
    low-level WebDAV client (app/documents/nextcloud_client.py), not a
    single global folder setting."""

    base_url: str | None = None
    username: str | None = None
    app_password: str | None = None

    model_config = SettingsConfigDict(env_prefix="CUE_NEXTCLOUD_", env_file=".env", extra="ignore")


class NextcloudAdapter:
    """The file_storage capability's FOSS substitute for SharePoint
    (CUE-PRD.md §9.3a), the inbound capture half — a file appearing or
    changing under `channel.external_ref` (a WebDAV folder path) is
    represented as a message-shaped envelope with no text and one media
    attachment, so it flows through the same queue/normaliser/media-
    pipeline path (items 3, 6) every other channel's attachments do. This
    is genuinely a different Protocol from app/documents/sharepoint.py's
    SharePointAdapter (write-back, one method, no read/list/health shape to
    reuse) — see that module and app/documents/nextcloud_client.py's own
    docstrings for why only the low-level WebDAV client is shared.

    `send()` raises NotImplementedError: a file-storage channel has no
    message-send concept, and failing loudly here is the same posture this
    package takes everywhere else rather than a silent no-op.
    """

    def __init__(self, settings: NextcloudCaptureSettings | None = None):
        settings = settings or NextcloudCaptureSettings()
        if not (settings.base_url and settings.username and settings.app_password):
            raise CaptureConfigError(
                "channel_types code 'nextcloud' requires CUE_NEXTCLOUD_BASE_URL, _USERNAME "
                "and _APP_PASSWORD"
            )
        self._client = NextcloudWebDavClient(settings.base_url, settings.username, settings.app_password)

    async def fetch_backlog(
        self, channel: Channel, since: datetime | None = None
    ) -> AsyncIterator[RawCapturedMessage]:
        folder = channel.external_ref or ""
        for entry in await self._client.propfind_changes_since(folder, since):
            yield _to_raw_message(folder, entry)

    async def stream(self, channel: Channel) -> AsyncIterator[RawCapturedMessage]:
        last_seen = datetime.now(timezone.utc)
        while True:
            async for message in self.fetch_backlog(channel, since=last_seen):
                last_seen = max(last_seen, message.sent_at)
                yield message
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)

    async def send(self, channel: Channel, to_external_id: str, text: str) -> None:
        raise NotImplementedError("file_storage channels have no message-send concept")

    async def health(self, channel: Channel) -> ChannelHealthResult:
        try:
            await self._client.propfind_changes_since(channel.external_ref or "", since=None)
            return ChannelHealthResult(healthy=True)
        except httpx.HTTPError as e:
            logger.warning("nextcloud health check failed for channel=%s: %s", channel.id, e)
            return ChannelHealthResult(healthy=False, detail={"error": str(e)})


def _to_raw_message(folder: str, entry) -> RawCapturedMessage:
    raw_bytes = entry.href.encode("utf-8")
    return RawCapturedMessage(
        external_id=entry.href,
        sender_external_id="",
        sent_at=entry.last_modified if entry.last_modified.tzinfo else entry.last_modified.replace(tzinfo=timezone.utc),
        text=None,
        raw_payload_hash=compute_payload_hash("nextcloud", entry.href, raw_bytes),
        media=[RawCapturedMedia(kind="document", uri=entry.href, filename=entry.display_name)],
    )
