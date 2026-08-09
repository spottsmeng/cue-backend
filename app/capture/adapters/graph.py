import asyncio
import logging
from datetime import datetime, timezone
from typing import AsyncIterator

import httpx

from app.capture.adapters.errors import CaptureConfigError
from app.capture.schema import ChannelHealthResult, RawCapturedMedia, RawCapturedMessage, compute_payload_hash
from app.core.graph_auth import GraphConfigError, GraphSettings, GraphTokenProvider, get_graph_settings
from app.models import Channel

logger = logging.getLogger("cue.capture.graph")

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_POLL_INTERVAL_SECONDS = 30


class GraphAdapter:
    """PRD §9.3: one Entra ID app registration, three capabilities
    (team_collaboration/`teams`, email/`outlook`, file_storage/`sharepoint`)
    — a single class branching on `channel.type` rather than three separate
    adapters, since it's genuinely one credential and one auth flow
    (app/core/graph_auth.py's GraphTokenProvider, shared with
    app/documents/sharepoint.py's write-back adapter). Code-complete,
    credential-blocked in this environment (Prompt 11's own stated scope,
    unchanged by this session) — swapping a project's `mattermost`/
    `imap_smtp`/`nextcloud` channel for the matching `teams`/`outlook`/
    `sharepoint` one, once Pico's app registration exists, is a
    channel_types/credentials configuration change (CUE-PRD.md §9.3a).

    `channel.external_ref` conventions per capability:
      teams:      "{team_id}/{channel_id}"
      outlook:    "{user_id}/{mail_folder_id}" (mail_folder_id defaults to "inbox")
      sharepoint: "{drive_id}" — capture-side; write-back stays
                  GraphSharePointAdapter (app/documents/sharepoint.py),
                  reused rather than duplicated.
    """

    def __init__(self, graph_settings: GraphSettings | None = None):
        try:
            self._token_provider = GraphTokenProvider(graph_settings or get_graph_settings())
        except GraphConfigError as e:
            raise CaptureConfigError(f"channel_types codes 'teams'/'outlook'/'sharepoint': {e}") from e

    async def _client(self) -> httpx.AsyncClient:
        token = await self._token_provider.acquire_token()
        return httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30)

    async def fetch_backlog(
        self, channel: Channel, since: datetime | None = None
    ) -> AsyncIterator[RawCapturedMessage]:
        if channel.type == "teams":
            async for message in self._fetch_teams(channel, since):
                yield message
        elif channel.type == "outlook":
            async for message in self._fetch_outlook(channel, since):
                yield message
        elif channel.type == "sharepoint":
            async for message in self._fetch_sharepoint(channel, since):
                yield message
        else:
            raise ValueError(f"GraphAdapter does not serve channel_types code {channel.type!r}")

    async def _fetch_teams(
        self, channel: Channel, since: datetime | None
    ) -> AsyncIterator[RawCapturedMessage]:
        team_id, channel_id = channel.external_ref.split("/", 1)
        async with await self._client() as client:
            response = await client.get(f"{GRAPH_BASE}/teams/{team_id}/channels/{channel_id}/messages")
            response.raise_for_status()
            for raw in response.json().get("value", []):
                sent_at = datetime.fromisoformat(raw["createdDateTime"].replace("Z", "+00:00"))
                if since is not None and sent_at <= since:
                    continue
                body = (raw.get("body") or {}).get("content", "")
                yield RawCapturedMessage(
                    external_id=raw["id"],
                    sender_external_id=((raw.get("from") or {}).get("user") or {}).get("id", ""),
                    sent_at=sent_at,
                    text=body,
                    raw_payload_hash=compute_payload_hash("teams", raw["id"], body.encode("utf-8")),
                )

    async def _fetch_outlook(
        self, channel: Channel, since: datetime | None
    ) -> AsyncIterator[RawCapturedMessage]:
        user_id, _, folder_id = channel.external_ref.partition("/")
        folder_id = folder_id or "inbox"
        params = {}
        if since is not None:
            params["$filter"] = f"receivedDateTime gt {since.isoformat()}"
        async with await self._client() as client:
            response = await client.get(
                f"{GRAPH_BASE}/users/{user_id}/mailFolders/{folder_id}/messages", params=params
            )
            response.raise_for_status()
            for raw in response.json().get("value", []):
                body = (raw.get("body") or {}).get("content", "")
                yield RawCapturedMessage(
                    external_id=raw["id"],
                    sender_external_id=((raw.get("from") or {}).get("emailAddress") or {}).get("address", ""),
                    sent_at=datetime.fromisoformat(raw["receivedDateTime"].replace("Z", "+00:00")),
                    text=body,
                    raw_payload_hash=compute_payload_hash("outlook", raw["id"], body.encode("utf-8")),
                )

    async def _fetch_sharepoint(
        self, channel: Channel, since: datetime | None
    ) -> AsyncIterator[RawCapturedMessage]:
        drive_id = channel.external_ref
        async with await self._client() as client:
            response = await client.get(f"{GRAPH_BASE}/drives/{drive_id}/root/delta")
            response.raise_for_status()
            for raw in response.json().get("value", []):
                if "file" not in raw:
                    continue  # folders/deletions carry no content to capture
                modified = raw.get("lastModifiedDateTime")
                sent_at = datetime.fromisoformat(modified.replace("Z", "+00:00")) if modified else datetime.now(timezone.utc)
                if since is not None and sent_at <= since:
                    continue
                yield RawCapturedMessage(
                    external_id=raw["id"],
                    sender_external_id=((raw.get("lastModifiedBy") or {}).get("user") or {}).get("id", ""),
                    sent_at=sent_at,
                    text=None,
                    raw_payload_hash=compute_payload_hash("sharepoint", raw["id"], raw["id"].encode("utf-8")),
                    media=[RawCapturedMedia(kind="document", uri=raw["id"], filename=raw.get("name"))],
                )

    async def stream(self, channel: Channel) -> AsyncIterator[RawCapturedMessage]:
        # Real-time delivery is Graph change-notification subscriptions
        # (webhooks) — out of this adapter's scope while credential-blocked
        # and untestable; polling fetch_backlog is the same deliberate
        # simplification MattermostAdapter/ImapSmtpAdapter/NextcloudAdapter
        # already make.
        last_seen = datetime.now(timezone.utc)
        while True:
            async for message in self.fetch_backlog(channel, since=last_seen):
                last_seen = max(last_seen, message.sent_at)
                yield message
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)

    async def send(self, channel: Channel, to_external_id: str, text: str) -> None:
        if channel.type == "sharepoint":
            raise NotImplementedError("file_storage channels have no message-send concept")
        async with await self._client() as client:
            if channel.type == "teams":
                team_id, channel_id = channel.external_ref.split("/", 1)
                response = await client.post(
                    f"{GRAPH_BASE}/teams/{team_id}/channels/{channel_id}/messages",
                    json={"body": {"content": text}},
                )
            else:  # outlook
                user_id = channel.external_ref.split("/", 1)[0]
                response = await client.post(
                    f"{GRAPH_BASE}/users/{user_id}/sendMail",
                    json={"message": {"subject": "CUE", "body": {"content": text}, "toRecipients": [
                        {"emailAddress": {"address": to_external_id}}
                    ]}},
                )
            response.raise_for_status()

    async def health(self, channel: Channel) -> ChannelHealthResult:
        try:
            async with await self._client() as client:
                response = await client.get(f"{GRAPH_BASE}/organization")
                response.raise_for_status()
            return ChannelHealthResult(healthy=True)
        except httpx.HTTPError as e:
            logger.warning("graph health check failed for channel=%s: %s", channel.id, e)
            return ChannelHealthResult(healthy=False, detail={"error": str(e)})

    async def fetch_media(self, channel: Channel, uri: str) -> bytes:
        """`sharepoint` only: `uri` is the driveItem id `_fetch_sharepoint`
        already yields, `/drives/{id}/items/{itemId}/content` is Graph's own
        download endpoint for it. `teams`/`outlook` don't populate
        RawCapturedMedia at all yet (`_fetch_teams`/`_fetch_outlook`
        above) — a bounded, documented gap (Teams hostedContents and
        Outlook message attachments are each a materially different Graph
        shape from driveItem content, and this whole adapter is
        credential-blocked/untested regardless), not silently pretended to
        work."""
        if channel.type != "sharepoint":
            raise NotImplementedError(
                f"GraphAdapter.fetch_media does not yet support channel_types code {channel.type!r}"
            )
        drive_id = channel.external_ref
        async with await self._client() as client:
            response = await client.get(f"{GRAPH_BASE}/drives/{drive_id}/items/{uri}/content")
            response.raise_for_status()
            return response.content
