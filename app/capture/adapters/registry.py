from functools import lru_cache

from app.capture.adapters.base import ChannelAdapter
from app.capture.adapters.fixtures_adapter import FixtureAdapter
from app.capture.adapters.graph import GraphAdapter
from app.capture.adapters.imap_smtp import ImapSmtpAdapter
from app.capture.adapters.mattermost import MattermostAdapter
from app.capture.adapters.nextcloud import NextcloudAdapter
from app.capture.adapters.wechat_work import WeChatWorkAdapter
from app.capture.adapters.whatsapp import WhatsAppAdapter
from app.capture.config import get_capture_settings

# One factory per live channel_types code — Graph serves three codes off
# one class/credential (app/capture/adapters/graph.py's own docstring).
# `manual` has no adapter at all: it's FR-LED-10's non-integration
# provenance value, never a real Channel a project attaches.
_LIVE_FACTORIES = {
    "whatsapp": WhatsAppAdapter,
    "wechat": WeChatWorkAdapter,
    "mattermost": MattermostAdapter,
    "imap_smtp": ImapSmtpAdapter,
    "nextcloud": NextcloudAdapter,
    "teams": GraphAdapter,
    "outlook": GraphAdapter,
    "sharepoint": GraphAdapter,
}


@lru_cache
def _fixture_adapter() -> FixtureAdapter:
    return FixtureAdapter()


@lru_cache
def _live_adapter(channel_type_code: str) -> ChannelAdapter:
    factory = _LIVE_FACTORIES.get(channel_type_code)
    if factory is None:
        raise ValueError(f"no live capture adapter registered for channel_type {channel_type_code!r}")
    # Each live adapter validates its own credentials at construction
    # (CaptureConfigError) — this raises immediately, not on first use, and
    # never silently falls back to the fixture adapter.
    return factory()


def get_adapter(channel_type_code: str) -> ChannelAdapter:
    """CUE_CAPTURE_BACKEND=fixture (default) is a single global switch, not
    a per-code one (app/capture/config.py's CaptureSettings docstring) —
    every channel_types code behaves identically against fixtures so the
    full test suite/CI never needs live credentials for any of them."""
    if get_capture_settings().backend == "fixture":
        return _fixture_adapter()
    return _live_adapter(channel_type_code)
