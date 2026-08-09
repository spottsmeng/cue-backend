from datetime import datetime
from typing import AsyncIterator, Protocol

from app.capture.schema import ChannelHealthResult, RawCapturedMessage
from app.models import Channel


class ChannelAdapter(Protocol):
    """Prompt 11 item 2's capture-side integration seam — one implementation
    per channel_types code (app/capture/adapters/registry.py), the same
    provider-pluggable shape app/llm/client.py's ModelClient and
    app/documents/storage.py's StorageBackend already establish.

    NFR-AVL-02/03's queue boundary is drawn here deliberately: an adapter's
    job ends at "here is a raw envelope" (fetch_backlog/stream yield
    RawCapturedMessage, never a Message ORM row) — the ingestion queue,
    normaliser and identity resolver (items 3-4) are queue consumers, not
    called inline from an adapter. If extraction throws downstream, the
    message the adapter already handed off is safe; it was never the
    adapter's problem.
    """

    async def fetch_backlog(
        self, channel: Channel, since: datetime | None = None
    ) -> AsyncIterator[RawCapturedMessage]:
        """FR-CAP-08's backfill path and item 11's scheduled-window trigger
        both call this — `since=None` means "everything the backend still
        has," not "nothing"."""
        ...

    async def stream(self, channel: Channel) -> AsyncIterator[RawCapturedMessage]:
        """Event-driven capture — a live subscription/poll loop, running
        for as long as the caller keeps consuming it."""
        ...

    async def send(self, channel: Channel, to_external_id: str, text: str) -> None:
        """FR-CAP-06/07's consent notice and the write-back session's
        in-language confirmation line both go through this. Raises
        NotImplementedError, not a silent no-op, for a channel whose
        capability has no message-send concept (e.g. file_storage)."""
        ...

    async def health(self, channel: Channel) -> ChannelHealthResult:
        """Item 9's active half of channel health — feeds the existing
        POST /channels/{id}/health receiving endpoint."""
        ...

    async def fetch_media(self, channel: Channel, uri: str) -> bytes:
        """Item 6's media pipeline (app/capture/media.py) — resolves a
        RawCapturedMedia.uri (an adapter-specific fetch reference: a
        Mattermost file id, a Nextcloud WebDAV href, a Graph driveItem id)
        into the original bytes, to be preserved via StorageBackend
        (FR-CAP-12: "at full fidelity"). Not part of item 2's original
        Protocol — item 2's own scope was fetch_backlog/stream/send/health;
        this method is item 6's addition, since nothing before it needed an
        adapter to resolve a media reference into bytes at all. Raises
        NotImplementedError for a channel/adapter with no way to fetch
        media (WeChat Work's Session Archiving media is end-to-end
        encrypted the same way its message text is — see
        WeChatWorkAdapter's own docstring; a fixture has no real media to
        fetch), the same "fail loudly, not silently" posture send() already
        establishes for a capability gap.
        """
        ...
