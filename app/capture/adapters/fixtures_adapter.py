import logging
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from typing import AsyncIterator

from app.capture.fixtures import load_fixture_cases
from app.capture.schema import (
    ChannelHealthResult,
    RawCapturedMedia,
    RawCapturedMessage,
    compute_payload_hash,
)
from app.models import Channel

logger = logging.getLogger("cue.capture.fixtures")

# A real, short, checked-in voice-note asset -- deliberately not added to
# cue-eval/cases.json. That file is the extraction eval's own tuned,
# hand-labelled regression corpus (CLAUDE.md: "cue-eval/ is the regression
# gate... change one rule at a time, re-run the full suite"); a media
# fixture is a different concern entirely and has no business inside a file
# whose whole point is measuring text-extraction accuracy against a fixed
# label set. Real speech (macOS `say`/`afconvert`, not a synthetic tone) --
# transcribing it exercises the real ASR client (app/capture/asr.py), not a
# stand-in. This is what closes the "transcript_confidence has no path to
# exist outside a live channel" gap: attach a WeChat channel with this exact
# external reference, Pull now, and this voice note runs through the
# identical real capture -> media -> ASR -> extraction pipeline a live one
# would. `wechat`, not `whatsapp` -- components/admin/project-channels-view.tsx's
# own docstring explains why: attaching a `whatsapp` channel replaces the
# free-text external-reference field with a real conversation picker (the
# Layer B Channel Picker work), so a typed sentinel like this one is only
# actually reachable through the product for a channel type that still
# keeps the plain field -- every type except whatsapp, wechat included.
#
# Deliberately gated on `external_ref`, not just `channel.type == "wechat"`
# -- several existing tests already attach a plain wechat channel and
# assert an exact fixture message count; making this unconditional for
# every wechat channel would have silently broken those and added a real
# ASR transcription to tests that have nothing to do with voice notes.
# Opt-in by name instead: zero existing test sees it, and a real UI user
# reaches it by attaching a channel with this reference, the same way any
# other fixture case is reached by picking a matching channel type.
_VOICE_NOTE_ASSET = Path(__file__).resolve().parent.parent / "fixtures_assets" / "sample_voice_note.wav"
_VOICE_NOTE_URI = "fixture-voice-note-01"
_VOICE_NOTE_CHANNEL_TYPE = "wechat"
_VOICE_NOTE_EXTERNAL_REF = "voice-demo"
_VOICE_NOTE_EXTERNAL_ID = "VN01"
_VOICE_NOTE_SENDER = "Ah Seng Production"
_VOICE_NOTE_SENT_AT = datetime(2026, 6, 23, 8, 15, tzinfo=dt_timezone.utc)


class FixtureAdapter:
    """The permanent dev/test backend for every channel_types code
    (CUE_CAPTURE_BACKEND=fixture, the default) -- cue-eval/cases.json's
    labelled cases, the same source app/capture/fixtures.py's docstring
    already names as the stand-in every prior session built against. Not
    replaced by real adapters, kept as one of several ChannelAdapter
    implementations, permanently.

    Fixtures have no live/backlog distinction of their own -- both
    `fetch_backlog` and `stream` yield the same case set for a channel's
    type, filtered by `since` where the case's own `sent_at` allows it.
    `send` logs only; `health` is always healthy -- there is no real
    connection to be unhealthy about.

    One addition past cases.json's own text-only cases: a `wechat` channel
    attached with external reference `"voice-demo"` also gets one real
    voice-note message (`_VOICE_NOTE_*` module constants below), with
    `fetch_media` resolving its real, checked-in audio -- the one media kind
    this adapter can actually produce bytes for.
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

        is_voice_demo_channel = (
            channel.type == _VOICE_NOTE_CHANNEL_TYPE and channel.external_ref == _VOICE_NOTE_EXTERNAL_REF
        )
        if is_voice_demo_channel and (since is None or _VOICE_NOTE_SENT_AT > since):
            yield RawCapturedMessage(
                external_id=_VOICE_NOTE_EXTERNAL_ID,
                sender_external_id=_VOICE_NOTE_SENDER,
                sent_at=_VOICE_NOTE_SENT_AT,
                text=None,
                raw_payload_hash=compute_payload_hash(
                    channel.type, _VOICE_NOTE_EXTERNAL_ID, _VOICE_NOTE_URI.encode("utf-8")
                ),
                media=[
                    RawCapturedMedia(
                        kind="voice_note", uri=_VOICE_NOTE_URI, filename="voice-note.wav"
                    )
                ],
            )

    async def send(self, channel: Channel, to_external_id: str, text: str) -> None:
        logger.info(
            "fixture send (no-op): channel=%s to=%r text=%r", channel.id, to_external_id, text
        )

    async def health(self, channel: Channel) -> ChannelHealthResult:
        return ChannelHealthResult(healthy=True, detail={"backend": "fixture"})

    async def fetch_media(self, channel: Channel, uri: str) -> bytes:
        if uri == _VOICE_NOTE_URI:
            return _VOICE_NOTE_ASSET.read_bytes()
        raise NotImplementedError("the fixture backend carries no real media to fetch")
