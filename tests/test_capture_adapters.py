"""No DB, no network — registry/provider resolution is pure config logic,
mirroring tests/test_llm_factory.py's own approach for app/llm/factory.py's
analogous provider-switch shape. FixtureAdapter behaviour (channel-type
filtering, deterministic dedup hashing) is exercised directly, no live
credentials needed for any of it — Prompt 11's own testing expectation.

Every settings getter here is @lru_cache'd (deliberately, same reasoning
test_llm_factory.py/test_sharepoint_adapter.py already give), as is the
registry's own _live_adapter — all cleared around each test.
"""

import uuid
from datetime import datetime, timezone

import pytest

from app.capture.adapters.errors import CaptureConfigError
from app.capture.adapters.fixtures_adapter import FixtureAdapter
from app.capture.adapters.mattermost import MattermostAdapter
from app.capture.adapters.registry import _fixture_adapter, _live_adapter, get_adapter
from app.capture.config import (
    get_capture_settings,
    get_imap_smtp_settings,
    get_mattermost_settings,
    get_wechat_work_settings,
    get_whatsapp_settings,
)
from app.capture.schema import compute_payload_hash
from app.core.graph_auth import get_graph_settings
from app.models import Channel


# Every one of these is a real, populated value in this machine's own .env
# (backend/PROGRESS.md's M8 notes — a live Nextcloud/Mattermost/GreenMail
# stack backs local dev now). pydantic-settings' env_file=".env" is a
# *fallback* below actual os.environ, not below "unset" (confirmed:
# monkeypatch.delenv alone does NOT hide a value .env provides — only an
# explicit os.environ entry, even "", outranks the dotenv file). Any test
# asserting "credentials absent" must blank these explicitly, or it becomes
# a test that only passes on a machine with an empty .env — the exact
# fragility this fixture exists to remove.
_CREDENTIAL_ENV_VARS = [
    "CUE_WHATSAPP_SESSION_ENDPOINT", "CUE_WHATSAPP_API_TOKEN",
    "CUE_WECHAT_CORP_ID", "CUE_WECHAT_CORP_SECRET", "CUE_WECHAT_SESSION_ARCHIVE_SECRET",
    "CUE_MATTERMOST_BASE_URL", "CUE_MATTERMOST_BOT_TOKEN",
    "CUE_IMAP_SMTP_IMAP_HOST", "CUE_IMAP_SMTP_SMTP_HOST",
    "CUE_IMAP_SMTP_USERNAME", "CUE_IMAP_SMTP_PASSWORD",
    "CUE_NEXTCLOUD_BASE_URL", "CUE_NEXTCLOUD_USERNAME", "CUE_NEXTCLOUD_APP_PASSWORD",
    "CUE_GRAPH_TENANT_ID", "CUE_GRAPH_CLIENT_ID", "CUE_GRAPH_CLIENT_SECRET",
]


@pytest.fixture(autouse=True)
def _clear_settings_cache(monkeypatch):
    getters = [
        get_capture_settings, get_whatsapp_settings, get_wechat_work_settings,
        get_mattermost_settings, get_imap_smtp_settings, get_graph_settings,
    ]
    for var in _CREDENTIAL_ENV_VARS:
        monkeypatch.setenv(var, "")
    for getter in getters:
        getter.cache_clear()
    _fixture_adapter.cache_clear()
    _live_adapter.cache_clear()
    yield
    for getter in getters:
        getter.cache_clear()
    _fixture_adapter.cache_clear()
    _live_adapter.cache_clear()


def _channel(channel_type: str, external_ref: str | None = None) -> Channel:
    return Channel(
        id=uuid.uuid4(), project_id=uuid.uuid4(), type=channel_type, external_ref=external_ref, healthy=True
    )


def test_default_backend_is_fixture_for_every_code(monkeypatch):
    monkeypatch.delenv("CUE_CAPTURE_BACKEND", raising=False)

    for code in ["whatsapp", "wechat", "mattermost", "imap_smtp", "nextcloud", "teams", "outlook", "sharepoint"]:
        assert isinstance(get_adapter(code), FixtureAdapter)


def test_live_backend_without_credentials_fails_loudly_per_adapter(monkeypatch):
    monkeypatch.setenv("CUE_CAPTURE_BACKEND", "live")

    for code in ["whatsapp", "wechat", "mattermost", "imap_smtp", "nextcloud", "teams", "outlook", "sharepoint"]:
        with pytest.raises(CaptureConfigError):
            get_adapter(code)


def test_live_backend_never_silently_falls_back_to_fixture(monkeypatch):
    """CUE_CAPTURE_BACKEND=live with credentials missing must raise, not
    quietly hand back a FixtureAdapter — the whole point of the single
    global switch (app/capture/config.py's CaptureSettings docstring)."""
    monkeypatch.setenv("CUE_CAPTURE_BACKEND", "live")

    with pytest.raises(CaptureConfigError):
        adapter = get_adapter("whatsapp")
        assert not isinstance(adapter, FixtureAdapter)


def test_live_backend_with_credentials_constructs_real_adapter(monkeypatch):
    monkeypatch.setenv("CUE_CAPTURE_BACKEND", "live")
    monkeypatch.setenv("CUE_MATTERMOST_BASE_URL", "https://mm.example.test")
    monkeypatch.setenv("CUE_MATTERMOST_BOT_TOKEN", "test-token")

    adapter = get_adapter("mattermost")

    assert isinstance(adapter, MattermostAdapter)


def test_live_backend_unknown_code_raises(monkeypatch):
    monkeypatch.setenv("CUE_CAPTURE_BACKEND", "live")

    with pytest.raises(ValueError, match="no live capture adapter registered"):
        get_adapter("manual")


@pytest.mark.asyncio
async def test_fixture_adapter_filters_by_channel_type():
    adapter = FixtureAdapter()

    whatsapp_messages = [m async for m in adapter.fetch_backlog(_channel("whatsapp"))]
    wechat_messages = [m async for m in adapter.fetch_backlog(_channel("wechat"))]

    assert whatsapp_messages, "cue-eval/cases.json has at least one whatsapp case"
    assert wechat_messages, "cue-eval/cases.json has at least one wechat case"
    assert all(m.raw_payload_hash for m in whatsapp_messages)


@pytest.mark.asyncio
async def test_fixture_adapter_respects_since():
    adapter = FixtureAdapter()
    all_messages = [m async for m in adapter.fetch_backlog(_channel("whatsapp"))]
    cutoff = max(m.sent_at for m in all_messages)

    later_only = [m async for m in adapter.fetch_backlog(_channel("whatsapp"), since=cutoff)]

    assert later_only == []


@pytest.mark.asyncio
async def test_fixture_adapter_health_is_always_healthy():
    adapter = FixtureAdapter()
    result = await adapter.health(_channel("whatsapp"))
    assert result.healthy is True


def test_payload_hash_is_deterministic_and_channel_scoped():
    """FR-CAP-11: a stable identity for 'the same message' independent of
    which channel path it arrived through — same inputs, same hash; a
    different external_id or channel_type changes it."""
    h1 = compute_payload_hash("whatsapp", "msg-1", b"hello")
    h2 = compute_payload_hash("whatsapp", "msg-1", b"hello")
    h3 = compute_payload_hash("whatsapp", "msg-2", b"hello")
    h4 = compute_payload_hash("wechat", "msg-1", b"hello")

    assert h1 == h2
    assert h1 != h3
    assert h1 != h4
