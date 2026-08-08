import hashlib
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class RawCapturedMedia:
    """A media attachment reference, not the bytes themselves — item 6's
    media pipeline (OCR/thumbnails/EXIF, StorageBackend-backed) resolves
    `uri` into stored bytes; the adapter's job ends at naming where to fetch
    it from."""

    kind: str  # "image" | "document" | "voice_note" | ...
    uri: str  # adapter-specific fetch reference (e.g. a Graph driveItem id, a Mattermost file id)
    filename: str | None = None


@dataclass
class RawCapturedMessage:
    """The raw envelope a ChannelAdapter hands to the ingestion queue —
    deliberately not the `Message` ORM row (Prompt 11 item 1): the
    normaliser (app/capture/normalise.py, a queue consumer, not inline in
    the adapter) is what turns this into one. `sender_external_id` is
    whatever identifier scheme the channel uses (phone / WeChat ID / UPN /
    email) — resolved to a Party by the identity resolver (item 4), not
    here. `raw_payload_hash` is FR-CAP-11's dedup key, computed the same way
    regardless of source channel via `compute_payload_hash` below."""

    external_id: str
    sender_external_id: str
    sent_at: datetime
    text: str | None
    raw_payload_hash: str
    media: list[RawCapturedMedia] = field(default_factory=list)


@dataclass
class ChannelHealthResult:
    """Mirrors app/api/schemas.py's ChannelHealthSignal shape so a
    ChannelAdapter's health() can be POSTed straight to the existing
    /channels/{id}/health receiving endpoint (item 9)."""

    healthy: bool
    detail: dict | None = None


def compute_payload_hash(channel_type: str, external_id: str, raw_bytes: bytes) -> str:
    """FR-CAP-11: a stable identity for "the same message" independent of
    which channel path it arrived through. Every adapter computes this the
    same way — channel_type + the channel's own external message id + the
    raw payload bytes — rather than each adapter inventing its own scheme,
    so dedup-across-paths (e.g. the same WhatsApp message re-delivered by a
    backfill and a live stream) actually collapses to one row."""
    digest = hashlib.sha256()
    digest.update(channel_type.encode("utf-8"))
    digest.update(b"\0")
    digest.update(external_id.encode("utf-8"))
    digest.update(b"\0")
    digest.update(raw_bytes)
    return digest.hexdigest()
