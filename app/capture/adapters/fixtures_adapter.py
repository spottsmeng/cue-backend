import logging
from datetime import datetime
from typing import AsyncIterator

from app.capture.fixtures import load_fixture_cases
from app.capture.schema import ChannelHealthResult, RawCapturedMessage, compute_payload_hash
from app.models import Channel

logger = logging.getLogger("cue.capture.fixtures")


class FixtureAdapter:
    """The permanent dev/test backend for every channel_types code
    (CUE_CAPTURE_BACKEND=fixture, the default) — cue-eval/cases.json's
    labelled cases, the same source app/capture/fixtures.py's docstring
    already names as the stand-in every prior session built against. Not
    replaced by real adapters, kept as one of several ChannelAdapter
    implementations, permanently.

    Fixtures have no live/backlog distinction of their own — both
    `fetch_backlog` and `stream` yield the same case set for a channel's
    type, filtered by `since` where the case's own `sent_at` allows it.
    `send` logs only; `health` is always healthy — there is no real
    connection to be unhealthy about.
    """

    async def fetch_backlog(
        self, channel: Channel, since: datetime | None = None
    ) -> AsyncIterator[RawCapturedMessage]:
        async for message in self._cases_for(channel, since):
            yield message

    async def stream(self, channel: Channel) -> AsyncIterator[RawCapturedMessage]:
        async for message in self._cases_for(channel, since=None):
            yield message

    async def _cases_for(
        self, channel: Channel, since: datetime | None
    ) -> AsyncIterator[RawCapturedMessage]:
        _project_context, cases = load_fixture_cases()
        for case in cases:
            if case["channel"] != channel.type:
                continue
            sent_at = datetime.fromisoformat(case["sent_at"])
            if since is not None and sent_at <= since:
                continue
            raw_bytes = case["message"].encode("utf-8")
            yield RawCapturedMessage(
                external_id=case["id"],
                sender_external_id=case["party"],
                sent_at=sent_at,
                text=case["message"],
                raw_payload_hash=compute_payload_hash(channel.type, case["id"], raw_bytes),
            )

    async def send(self, channel: Channel, to_external_id: str, text: str) -> None:
        logger.info(
            "fixture send (no-op): channel=%s to=%r text=%r", channel.id, to_external_id, text
        )

    async def health(self, channel: Channel) -> ChannelHealthResult:
        return ChannelHealthResult(healthy=True, detail={"backend": "fixture"})

    async def fetch_media(self, channel: Channel, uri: str) -> bytes:
        raise NotImplementedError("the fixture backend carries no real media to fetch")
